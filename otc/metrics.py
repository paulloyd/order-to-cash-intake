"""KPIs (from the database) and evaluation (against synthetic ground truth), plus the simulated human/customer step.

Two kinds of numbers are produced and they are labelled differently everywhere they appear:
  MEASURED  - counted from what the pipeline actually did on the synthetic orders
  MODELED   - derived from editable assumptions in config.ASSUMPTIONS (e.g. rep minutes per order)
"""
import json, re
from datetime import datetime, timedelta
from collections import Counter

from . import config, pipeline
from .db import q, one


# ------------------------------------------------------------------ simulated humans (demo only)
def _override_for(exc, t, rng):
    code, ln = exc["code"], exc["line_no"]
    la = t["lines_all"]
    if code == "MISSING_PO":
        return {"code": code, "value": t["po_true"]}
    if code == "UNKNOWN_CUSTOMER":
        return {"code": code, "value": t["true_account"]}
    if code in ("UNKNOWN_SKU", "AMBIGUOUS_SKU") and ln and ln <= len(la):
        return {"code": code, "line_no": ln, "value": la[ln - 1]["sku"]}
    if code in ("MISSING_QTY", "BELOW_MOQ", "PACK_MULTIPLE") and ln and ln <= len(la):
        return {"code": code, "line_no": ln, "value": la[ln - 1]["fix_qty"]}
    if code == "PAST_DATE":
        return {"code": code, "value": t["fix_need"]}
    if code in ("MISSING_SHIP_TO", "SHIP_TO_UNRECOGNIZED"):
        return {"code": code, "value": t["ship_true"]}
    if code == "PRICE_MISMATCH" and ln:
        return {"code": code, "line_no": ln, "value": "contract"}
    if code in ("CREDIT_HOLD", "CREDIT_LIMIT"):
        return {"code": code, "value": "approved:finance"} if rng.random() < 0.5 else None   # half stay pending with finance
    if code == "DUPLICATE_PO":
        return {"code": code, "value": "discard"}
    return None


def simulate_resolutions(db, truth, rng):
    """Demo-only: reps send the drafted clarification and apply fixes; customers answer with the true values."""
    for t in truth:
        mid = t.get("case_id")
        for _ in range(3):
            case = one(db, "SELECT * FROM messages WHERE id=?", (mid,))
            if not case or case["status"] not in ("NEEDS_REVIEW", "AWAITING_CUSTOMER"):
                break
            at = datetime.fromisoformat(case["received_at"]) + timedelta(minutes=rng.randint(20, 300))
            blocking = q(db, "SELECT * FROM exceptions WHERE message_id=? AND status='open' AND severity='block'", (mid,))
            for d in q(db, "SELECT id FROM outbound_emails WHERE message_id=? AND kind='clarification' AND status='draft'", (mid,)):
                pipeline.send_draft(db, d["id"], actor="rep", now=at)
            ovs = [o for o in (_override_for(e, t, rng) for e in blocking) if o]
            if not ovs:
                break
            pipeline.apply_overrides(db, mid, ovs, actor="rep/customer(simulated)", now=at + timedelta(minutes=5))
    db.commit()


# ------------------------------------------------------------------ KPIs
def kpis(db, assumptions=None):
    a = {**config.ASSUMPTIONS, **(assumptions or {})}
    cases = q(db, "SELECT * FROM messages")
    n = len(cases)
    by_status = Counter(c["status"] for c in cases)
    touchless = sum(c["touchless"] for c in cases)
    confirmed = by_status["CONFIRMED"] + by_status["SHIPPED"]
    assisted = confirmed - touchless
    open_ = by_status["NEEDS_REVIEW"] + by_status["AWAITING_CUSTOMER"]

    first = q(db, "SELECT e.*, m.extracted_json FROM exceptions e JOIN messages m ON m.id=e.message_id WHERE e.run_no=1")
    by_code = Counter((e["code"], e["severity"]) for e in first)
    sent = q(db, "SELECT body, message_id FROM outbound_emails WHERE kind='clarification' AND status='sent'")
    questions = sum(len(re.findall(r"^\d+\. ", e["body"], re.M)) for e in sent)
    n_clar = len(sent)

    per_case_issues = Counter(e["message_id"] for e in first if e["path"] == "customer" and e["severity"] == "block")
    manual = automated = 0.0
    for c in cases:
        lines = len(json.loads(c["extracted_json"])["lines"])
        manual += a["manual_min_base"] + a["manual_min_per_line"] * lines + a["manual_min_per_customer_email"] * per_case_issues[c["id"]]
        automated += a["monitor_min_per_touchless_case"] if c["touchless"] else a["review_min_per_assisted_case"]
    value = (one(db, "SELECT COALESCE(SUM(total),0) v FROM orders") or {"v": 0})["v"]
    return {
        "cases": n, "by_status": dict(by_status), "by_channel": dict(Counter(c["channel"] for c in cases)),
        "touchless": touchless, "touchless_rate": round(touchless / n, 3) if n else 0,
        "assisted_confirmed": assisted, "open": open_, "discarded": by_status["DISCARDED"],
        "avg_processing_ms_first_pass": round(sum(c["processing_ms"] for c in cases) / n, 1) if n else 0,
        "extractor_used": dict(Counter(c["extractor"] for c in cases)),
        "clarification_emails_sent": n_clar, "customer_questions_bundled": questions,
        "avg_questions_per_clarification_email": round(questions / n_clar, 2) if n_clar else 0,
        "emails_avoided_by_bundling_vs_one_per_issue": questions - n_clar,
        "customer_emails_per_case": round((n_clar + confirmed) / n, 2) if n else 0,
        "exceptions_first_pass": [{"code": c, "severity": s, "count": k} for (c, s), k in by_code.most_common()],
        "confirmed_order_value": round(value, 2),
        "hubspot_deals": one(db, "SELECT COUNT(*) c FROM mock_hs_deals")["c"],
        "shipped": by_status["SHIPPED"],
        "MODELED_rep_minutes_manual": round(manual, 1), "MODELED_rep_minutes_with_automation": round(automated, 1),
        "MODELED_rep_minutes_saved": round(manual - automated, 1),
        "MODELED_assumptions": a,
    }


# ------------------------------------------------------------------ evaluation vs ground truth
def evaluate(db, truth):
    tp = fp = fn = exact = tl_ok = cust_ok = cust_n = 0
    lines_ok = lines_n = 0
    per_defect, misses = {}, []
    n = 0
    for t in truth:
        mid = t.get("case_id")
        if not mid:
            continue
        n += 1
        found = {r["code"] for r in q(db, "SELECT code FROM exceptions WHERE message_id=? AND run_no=1", (mid,))}
        exp = set(t["expected_codes"])
        tp += len(found & exp)
        fp += len(found - exp)
        fn += len(exp - found)
        ok = found == exp
        exact += ok
        blocked = bool(q(db, "SELECT 1 FROM exceptions WHERE message_id=? AND run_no=1 AND severity='block'", (mid,)))
        tl_ok += (not blocked) == t["expect_touchless"]
        for d in t["defects"]:
            c = per_defect.setdefault(d, [0, 0])
            c[1] += 1
            c[0] += int(any(x in found for x in [d.upper(), {"description_only": "UNKNOWN_SKU", "stock_shortage": "INSUFFICIENT_STOCK"}.get(d, "")]) or
                        {"description_only": "UNKNOWN_SKU"}.get(d, d.upper()) in found)
        if t["customer"]:
            cust_n += 1
            row = one(db, "SELECT c.account_no a FROM messages m LEFT JOIN customers c ON c.id=m.customer_id WHERE m.id=?", (mid,))
            cust_ok += int(row and row["a"] == t["customer"])
        if t["expect_touchless"]:
            order = one(db, "SELECT id FROM orders WHERE message_id=?", (mid,))
            lines_n += 1
            got = sorted([r["sku"], r["qty"]] for r in q(db, "SELECT sku, qty FROM order_lines WHERE order_id=?", (order["id"],))) if order else None
            lines_ok += int(got == sorted(t["expected_lines"]))
        if not ok and len(misses) < 12:
            misses.append({"case": one(db, "SELECT ref FROM messages WHERE id=?", (mid,))["ref"], "file": t["file"],
                           "expected": sorted(exp), "found": sorted(found), "defects": t["defects"]})
    prec = tp / (tp + fp) if tp + fp else 1.0
    rec = tp / (tp + fn) if tp + fn else 1.0
    return {"orders_evaluated": n, "exception_code_precision": round(prec, 3), "exception_code_recall": round(rec, 3),
            "exact_exception_set_match": f"{exact}/{n}", "touchless_decision_agreement": f"{tl_ok}/{n}",
            "customer_identified_correctly": f"{cust_ok}/{cust_n}",
            "clean_orders_created_with_correct_lines": f"{lines_ok}/{lines_n}",
            "detection_by_injected_defect": {d: f"{a}/{b}" for d, (a, b) in sorted(per_defect.items())},
            "mismatches_sample": misses,
            "note": "Measured on synthetic orders generated by the same author - a regression benchmark, not a claim about real-world accuracy."}


def render(k, ev):
    s = k["by_status"]
    L = ["", "=== ORDER-TO-CASH INTAKE - DEMO RESULTS (synthetic data) ===", "",
         f"Cases processed          : {k['cases']}   channels {k['by_channel']}",
         f"Touchless (first pass)   : {k['touchless']}  ({k['touchless_rate']:.0%})",
         f"Confirmed after review   : {k['assisted_confirmed']}",
         f"Still open               : {k['open']}   discarded duplicates: {k['discarded']}",
         f"Shipped (via ERP sync)   : {k['shipped']}   HubSpot deals: {k['hubspot_deals']}",
         f"Avg pipeline time        : {k['avg_processing_ms_first_pass']} ms per case (extraction={k['extractor_used']})",
         f"Clarification emails     : {k['clarification_emails_sent']} sent, bundling {k['customer_questions_bundled']} questions "
         f"({k['avg_questions_per_clarification_email']} per email) -> {k['emails_avoided_by_bundling_vs_one_per_issue']} fewer emails than one-per-issue",
         f"Customer emails per case : {k['customer_emails_per_case']}",
         f"Confirmed order value    : ${k['confirmed_order_value']:,.2f}",
         "", "MODELED (assumption-based, editable): rep minutes "
         f"{k['MODELED_rep_minutes_manual']} manual vs {k['MODELED_rep_minutes_with_automation']} with automation "
         f"= {k['MODELED_rep_minutes_saved']} saved", "",
         "--- Measured vs ground truth ---",
         f"Exception precision / recall : {ev['exception_code_precision']} / {ev['exception_code_recall']}",
         f"Exact exception-set match    : {ev['exact_exception_set_match']}",
         f"Touchless decision agreement : {ev['touchless_decision_agreement']}",
         f"Customer identified          : {ev['customer_identified_correctly']}",
         f"Clean orders w/ correct lines: {ev['clean_orders_created_with_correct_lines']}",
         "Detection by injected defect : " + ", ".join(f"{d} {v}" for d, v in ev["detection_by_injected_defect"].items())]
    if ev["mismatches_sample"]:
        L += ["", "Mismatches (first few):"] + [f"  {m['case']} {m['file']}: expected {m['expected']} found {m['found']}" for m in ev["mismatches_sample"][:8]]
    return "\n".join(L)
