import json, sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from otc import config, db as D, seed, pipeline, connectors, extract
from otc.validate import resolve_sku


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "EXTRACTOR_MODE", "rules")
    d = D.connect(tmp_path / "t.db")
    seed.build(d)
    return d


def customer(db, **where):
    """A normal customer: single site (no ship-to ambiguity), not on hold, PO required."""
    c = D.one(db, "SELECT c.* FROM customers c WHERE credit_hold=0 AND requires_po=1 AND discount_pct=0 AND "
                  "balance < credit_limit*0.3 AND (SELECT COUNT(*) FROM ship_to s WHERE s.customer_id=c.id)=1 "
                  "AND NOT EXISTS (SELECT 1 FROM customer_xref x WHERE x.customer_id=c.id) LIMIT 1")
    c["email"] = D.one(db, "SELECT email FROM contacts WHERE customer_id=?", (c["id"],))["email"]
    return c


def stocked(db, n=3):
    return [r["sku"] for r in D.q(db, "SELECT i.sku FROM items i JOIN inventory v ON v.sku=i.sku WHERE i.moq=1 AND i.pack_multiple=1 "
                                      "AND v.on_hand-v.reserved>=200 LIMIT ?", (n,))]


def send(db, c, body, subject="Order", **kw):
    mid = pipeline.ingest_email(db, c["email"], subject, body, **kw)
    return D.one(db, "SELECT * FROM messages WHERE id=?", (mid,))


def codes(db, mid, run=1):
    return {r["code"] for r in D.q(db, "SELECT code FROM exceptions WHERE message_id=? AND run_no=?", (mid, run))}


def test_normalised_sku_matching(db):
    sku = stocked(db, 1)[0]
    item, how = resolve_sku(db, sku.lower().replace("-", " ").replace(" ", ""), None)
    assert item["sku"] == sku and how == "exact"


def test_clean_order_is_touchless_and_creates_everything(db):
    c, (a, b, _) = customer(db), stocked(db)
    m = send(db, c, f"PO# NW-10001\nNeed by 2099-01-15\n\n10 x {a}\n{b} - 5 pcs\n")
    assert m["status"] == "CONFIRMED" and m["touchless"] == 1
    assert D.one(db, "SELECT COUNT(*) n FROM mock_bc_sales_orders")["n"] == 1
    assert D.one(db, "SELECT COUNT(*) n FROM mock_hs_deals")["n"] == 1
    mails = D.q(db, "SELECT kind FROM outbound_emails WHERE message_id=?", (m["id"],))
    assert [x["kind"] for x in mails] == ["confirmation"]          # exactly one email to the customer


def test_near_miss_part_number_is_never_auto_corrected(db):
    c, (a, *_ ) = customer(db), stocked(db)
    m = send(db, c, f"PO# NW-10002\n10 x {a[:-1]}5\n")
    assert m["status"] == "NEEDS_REVIEW" and "AMBIGUOUS_SKU" in codes(db, m["id"])
    assert D.one(db, "SELECT COUNT(*) n FROM mock_bc_sales_orders")["n"] == 0


def test_several_questions_are_bundled_into_one_email(db):
    c, (a, b, _) = customer(db), stocked(db)
    m = send(db, c, f"10 x {a}\n{b}\n8 pcs of 1/2 inch stainless elbow\n")      # no PO, no qty, unidentifiable item
    drafts = D.q(db, "SELECT * FROM outbound_emails WHERE message_id=? AND kind='clarification'", (m["id"],))
    assert len(drafts) == 1 and drafts[0]["status"] == "draft"
    assert drafts[0]["body"].count("\n1. ") == 1 and "\n3. " in drafts[0]["body"]
    assert {"MISSING_PO", "MISSING_QTY", "UNKNOWN_SKU"} <= codes(db, m["id"])


def test_rep_overrides_reprocess_to_confirmed_but_not_touchless(db):
    c, (a, b, _) = customer(db), stocked(db)
    m = send(db, c, f"10 x {a}\n{b}\n")
    pipeline.apply_overrides(db, m["id"], [{"code": "MISSING_PO", "value": "NW-777"},
                                           {"code": "MISSING_QTY", "line_no": 2, "value": 4}], actor="rep")
    m = D.one(db, "SELECT * FROM messages WHERE id=?", (m["id"],))
    assert m["status"] == "CONFIRMED" and m["touchless"] == 0 and m["po_number"] == "NW-777"
    assert D.one(db, "SELECT COUNT(*) n FROM audit_log WHERE action='override'")["n"] == 2


def test_credit_hold_blocks_until_approved(db):
    c, (a, *_ ) = customer(db), stocked(db)
    db.execute("UPDATE customers SET credit_hold=1 WHERE id=?", (c["id"],))
    m = send(db, c, f"PO# NW-10003\n5 x {a}\n")
    assert m["status"] == "NEEDS_REVIEW" and "CREDIT_HOLD" in codes(db, m["id"])
    pipeline.apply_overrides(db, m["id"], [{"code": "CREDIT_HOLD", "value": "approved:finance"}])
    assert D.one(db, "SELECT status FROM messages WHERE id=?", (m["id"],))["status"] == "CONFIRMED"


def test_duplicate_po_is_caught(db):
    c, (a, *_ ) = customer(db), stocked(db)
    first = send(db, c, f"PO# NW-10004\n5 x {a}\n")
    again = send(db, c, f"Resending PO# NW-10004\n5 x {a}\n", subject="Order again")
    assert first["status"] == "CONFIRMED" and "DUPLICATE_PO" in codes(db, again["id"])


def test_quoted_reply_and_signature_noise_are_ignored(db):
    c, (a, *_ ) = customer(db), stocked(db)
    m = send(db, c, f"PO# NW-10005\n5 x {a}\n\n> On Mon wrote:\n> 99 x ZZ-0000-QQ\n\n-----Original Message-----\n50 x YY-1111-AA\n")
    assert m["status"] == "CONFIRMED"
    assert D.one(db, "SELECT COUNT(*) n FROM order_lines")["n"] == 1


def test_erp_refuses_duplicate_external_document_number(db):
    bc = connectors.MockBC(db)
    c = customer(db)
    payload = connectors.bc_payload(c["account_no"], "NW-1", "2099-01-01", [{"sku": stocked(db, 1)[0], "qty": 1, "unit_price": 1.0}])
    bc.create_sales_order(payload)
    with pytest.raises(connectors.IntegrationError):
        bc.create_sales_order(payload)


def test_crm_outage_never_blocks_or_loses_an_order(db, monkeypatch):
    class Down:
        def upsert_contact(self, *a, **k): raise RuntimeError("HubSpot 503")
    monkeypatch.setattr(pipeline, "get_hubspot", lambda _db: Down())
    c, (a, *_ ) = customer(db), stocked(db)
    m = send(db, c, f"PO# NW-10006\n5 x {a}\n")
    o = D.one(db, "SELECT * FROM orders WHERE message_id=?", (m["id"],))
    assert m["status"] == "CONFIRMED" and "HubSpot sync failed" in o["sync_note"]


def test_ambiguous_wording_fails_safe_instead_of_guessing(db):
    c = customer(db)
    m1 = send(db, c, "Same as last time please, PO NW-55555")
    assert "NO_LINES" in codes(db, m1["id"]) and m1["status"] == "NEEDS_REVIEW"
    a = stocked(db, 1)[0]
    m2 = send(db, c, f"Need 2 dozen {a} PO NW-55556")
    assert "MISSING_QTY" in codes(db, m2["id"]) and m2["status"] == "NEEDS_REVIEW"


def test_llm_failure_falls_back_to_rules(db, monkeypatch):
    def boom(*a, **k): raise TimeoutError("api down")
    monkeypatch.setattr(extract, "llm_extract", boom)
    ex = extract.extract("a@b.com", "s", "PO NW-1\n5 x HF-0412-SS", mode="llm")
    assert ex.extractor == "rules(fallback)" and ex.lines[0].qty == 5


def test_price_variance_needs_approval_and_stock_shortfall_only_warns(db):
    c = customer(db)
    sku = stocked(db, 1)[0]
    price = D.one(db, "SELECT list_price p FROM items WHERE sku=?", (sku,))["p"]
    m = send(db, c, f"Purchase Order: NW-10007\n{sku} qty 5 @ ${price * 0.8:.2f}\n")
    assert "PRICE_MISMATCH" in codes(db, m["id"]) and m["status"] == "NEEDS_REVIEW"
    low = D.one(db, "SELECT i.sku FROM items i JOIN inventory v ON v.sku=i.sku WHERE i.moq=1 AND i.pack_multiple=1 AND v.on_hand-v.reserved=0 LIMIT 1")
    if low:
        m2 = send(db, c, f"PO# NW-10008\n5 x {low['sku']}\n")
        assert m2["status"] == "CONFIRMED" and "INSUFFICIENT_STOCK" in codes(db, m2["id"])


def test_customer_status_lookup_requires_matching_email(db):
    c, (a, *_ ) = customer(db), stocked(db)
    m = send(db, c, f"PO# NW-10009\n5 x {a}\n")
    assert pipeline.case_status(db, m["ref"], c["email"])["status"] == "CONFIRMED"
    assert pipeline.case_status(db, m["ref"], "stranger@elsewhere.example") is None


def test_form_and_magento_use_the_same_pipeline(db, tmp_path):
    c, (a, *_ ) = customer(db), stocked(db)
    mid = pipeline.ingest_form(db, c["email"], "NW-10010", None, None, [[a, 5]])
    assert D.one(db, "SELECT status FROM messages WHERE id=?", (mid,))["status"] == "CONFIRMED"
    price = D.one(db, "SELECT list_price p FROM items WHERE sku=?", (a,))["p"]
    (tmp_path / "magento_orders.json").write_text(json.dumps([{
        "increment_id": "MG-1", "created_at": "2026-10-14T10:00:00", "customer_email": c["email"], "payment": {"po_number": "NW-10011"},
        "items": [{"sku": a, "qty_ordered": 3, "price": price}], "extension_attributes": {"shipping_assignments": []}}]))
    ids = pipeline.pull_magento(db)
    assert D.one(db, "SELECT status FROM messages WHERE id=?", (ids[0],))["status"] == "CONFIRMED"
    assert pipeline.pull_magento(db) == []                              # already imported -> idempotent


def test_end_to_end_benchmark_regression(tmp_path, monkeypatch):
    """Regression guard on the synthetic benchmark (same author generated and parsed it - NOT real-world accuracy)."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(config, "REPORT_DIR", tmp_path / "reports")
    monkeypatch.setattr(config, "EXTRACTOR_MODE", "rules")
    import run_demo
    _, k, ev = run_demo.main(tmp_path / "demo.db", quiet=True)
    assert k["cases"] == 100
    assert ev["exception_code_recall"] >= 0.95 and ev["exception_code_precision"] >= 0.95
