"""Orchestration: intake -> extract -> validate -> (create order | exception queue) -> notify -> sync.

Case statuses: NEEDS_REVIEW -> AWAITING_CUSTOMER -> CONFIRMED -> SHIPPED, or DISCARDED.
Every state change is written to status_history; every human or system decision to audit_log.
"""
import hashlib, io, json, re
from datetime import datetime, date
from email import policy
from email.parser import BytesParser
from time import perf_counter

from . import config, comms
from .connectors import IntegrationError, bc_payload, get_bc, get_hubspot, get_magento
from .db import audit, now_iso, one, q
from .extract import extract
from .models import Exc, Extracted, Line
from .validate import validate


def pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
        return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)
    except Exception:
        return ""


def _set_status(db, mid, status, note="", at=None):
    at = now_iso(at)
    db.execute("UPDATE messages SET status=?, updated_at=? WHERE id=?", (status, at, mid))
    db.execute("INSERT INTO status_history(message_id,status,note,at) VALUES (?,?,?,?)", (mid, status, note, at))


# ------------------------------------------------------------------ intake
def ingest(db, channel, sender, subject, raw_text, received_at=None, extracted: Extracted = None, process=True, mode=None):
    received_at = received_at or datetime.utcnow()
    h = hashlib.sha256(f"{channel}|{sender}|{subject}|{raw_text}".encode()).hexdigest()
    dup = one(db, "SELECT id FROM messages WHERE content_hash=?", (h,))
    if dup:
        return dup["id"]
    ex = extracted or extract(sender, subject, raw_text, mode)
    cur = db.execute("INSERT INTO messages(channel,received_at,sender,subject,raw_text,content_hash,extracted_json,extractor,status,updated_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (channel, now_iso(received_at), sender, subject, raw_text, h, ex.to_json(), ex.extractor, "RECEIVED", now_iso(received_at)))
    mid = cur.lastrowid
    db.execute("UPDATE messages SET ref=? WHERE id=?", (f"OTC-{mid:06d}", mid))
    db.execute("INSERT INTO status_history(message_id,status,note,at) VALUES (?,?,?,?)", (mid, "RECEIVED", f"via {channel}", now_iso(received_at)))
    audit(db, "system", "ingest", f"OTC-{mid:06d}", f"{channel} from {sender}; extractor={ex.extractor}; {len(ex.lines)} lines", now_iso(received_at))
    if process:
        process_case(db, mid, now=received_at)
    db.commit()
    return mid


def ingest_email(db, sender, subject, body, attachments=(), received_at=None, mode=None):
    text = body
    for name, data in attachments:
        if name.lower().endswith(".pdf"):
            text += f"\n\n[attachment: {name}]\n{pdf_text(data)}"
    return ingest(db, "email", sender, subject, text, received_at, mode=mode)


def ingest_eml_file(db, path, mode=None):
    msg = BytesParser(policy=policy.default).parse(open(path, "rb"))
    body = msg.get_body(preferencelist=("plain",))
    atts = [(p.get_filename(), p.get_payload(decode=True)) for p in msg.iter_attachments() if p.get_filename()]
    try:
        rec = datetime(*msg["Date"].datetime.timetuple()[:6])
    except Exception:
        rec = datetime.utcnow()
    sender = re.sub(r".*<(.+)>.*", r"\1", str(msg["From"]))
    return ingest_email(db, sender, str(msg["Subject"] or ""), body.get_content() if body else "", atts, rec, mode)


def ingest_form(db, sender, po_number, requested_date, ship_to_code, lines, received_at=None):
    """Customer-portal order form: already structured, so no extraction step is needed."""
    ex = Extracted(lines=[Line(i, f"{s} x{q_}", s, int(q_)) for i, (s, q_) in enumerate(lines, 1)],
                   po_number=po_number or None, requested_date=requested_date or None, ship_to_text=ship_to_code or None,
                   customer_hint={"email": sender}, extractor="structured")
    return ingest(db, "web_form", sender, f"Portal order {po_number or ''}".strip(), json.dumps(
        {"po": po_number, "date": requested_date, "ship_to": ship_to_code, "lines": lines}), received_at, ex)


def pull_magento(db, now=None):
    """Poll Magento for new web orders and push them through the SAME validation pipeline."""
    mg, ids = get_magento(db), []
    for o in mg.fetch_new_orders():
        addr = ((o.get("extension_attributes") or {}).get("shipping_assignments") or [{}])[0].get("shipping", {}).get("address", {})
        ship = " ".join(filter(None, [" ".join(addr.get("street", [])), addr.get("city"), addr.get("region_code"), addr.get("postcode")])) or None
        ex = Extracted(lines=[Line(i, f"{it['sku']} x{int(it['qty_ordered'])}", it["sku"], int(it["qty_ordered"]), float(it.get("price") or 0) or None)
                              for i, it in enumerate(o["items"], 1)],
                       po_number=(o.get("payment") or {}).get("po_number"), ship_to_text=ship,
                       customer_hint={"email": o.get("customer_email")}, extractor="structured")
        rec = datetime.fromisoformat(o["created_at"]) if o.get("created_at") else (now or datetime.utcnow())
        mid = ingest(db, "magento", o.get("customer_email"), f"Magento order {o['increment_id']}", json.dumps(o), rec, ex)
        db.execute("INSERT OR IGNORE INTO mock_magento_imported VALUES (?,?)", (o["increment_id"], mid))
        ids.append(mid)
    db.commit()
    return ids


# ------------------------------------------------------------------ processing
def process_case(db, mid, now=None, actor="system"):
    t0 = perf_counter()
    case = one(db, "SELECT * FROM messages WHERE id=?", (mid,))
    ex = Extracted.from_json(case["extracted_json"])
    overrides = json.loads(case["overrides_json"])
    res = validate(db, ex, case["raw_text"], overrides, date.fromisoformat(case["received_at"][:10]), mid)
    run_no = case["run_no"] + 1
    db.execute("UPDATE exceptions SET status='superseded' WHERE message_id=? AND status='open'", (mid,))

    order_info = None
    if res.discard:
        _store_exceptions(db, mid, run_no, res.exceptions)
        _finish(db, mid, case, res, run_no, "DISCARDED", "Duplicate discarded by rep", now, t0)
        return {"status": "DISCARDED"}
    if not res.blocking:
        try:
            order_info = _create_order(db, case, res, now)
        except IntegrationError as e:
            res.exceptions.append(Exc("INTEGRATION_ERROR", "block", "internal", str(e)))
    _store_exceptions(db, mid, run_no, res.exceptions)

    if order_info:
        _finish(db, mid, case, res, run_no, "CONFIRMED", f"Created {order_info['bc_order_no']}", now, t0,
                touchless=int(run_no == 1 and not overrides))
    else:
        _finish(db, mid, case, res, run_no, "NEEDS_REVIEW",
                ", ".join(sorted({e.code for e in res.exceptions if e.severity == 'block'})), now, t0)
        case = one(db, "SELECT * FROM messages WHERE id=?", (mid,))
        stored = q(db, "SELECT * FROM exceptions WHERE message_id=? AND status='open'", (mid,))
        comms.draft_clarification(db, case, stored, at=now_iso(now))
    db.commit()
    return {"status": one(db, "SELECT status FROM messages WHERE id=?", (mid,))["status"],
            "exceptions": [e.code for e in res.exceptions]}


def _store_exceptions(db, mid, run_no, excs):
    for e in excs:
        db.execute("INSERT INTO exceptions(message_id,run_no,code,severity,path,line_no,message,question,fix_json,status) "
                   "VALUES (?,?,?,?,?,?,?,?,?,'open')",
                   (mid, run_no, e.code, e.severity, e.path, e.line_no, e.message, e.question, json.dumps(e.fix)))


def _finish(db, mid, case, res, run_no, status, note, now, t0, touchless=0):
    ms = (perf_counter() - t0) * 1000 if run_no == 1 else case["processing_ms"]
    db.execute("UPDATE messages SET customer_id=?, po_number=?, run_no=?, processing_ms=?, touchless=? WHERE id=?",
               (res.customer["id"] if res.customer else None, res.po_number, run_no, ms, touchless, mid))
    _set_status(db, mid, status, note, now)


def _create_order(db, case, res, now):
    cust, lines = res.customer, res.lines
    total = res.order_value
    payload = bc_payload(cust["account_no"], res.po_number, res.requested_date, lines)
    bc = get_bc(db)
    so = bc.create_sales_order(payload, at=now_iso(now))          # may raise IntegrationError
    cur = db.execute("INSERT INTO orders(message_id,customer_id,po_number,requested_date,ship_to_code,bc_order_no,total,created_at) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     (case["id"], cust["id"], res.po_number, res.requested_date, res.ship_to["code"] if res.ship_to else None,
                      so["number"], total, now_iso(now)))
    oid = cur.lastrowid
    for l in lines:
        db.execute("INSERT INTO order_lines(order_id,line_no,sku,qty,unit_price,backorder_qty) VALUES (?,?,?,?,?,?)",
                   (oid, l["line_no"], l["sku"], l["qty"], l["unit_price"], l["backorder_qty"]))
    audit(db, "system", "bc_order_created", case["ref"], f"{so['number']} total ${total:,.2f}", now_iso(now))

    note = ""
    try:                                                          # CRM failure must never lose or block an order
        hs = get_hubspot(db)
        contact = one(db, "SELECT name FROM contacts WHERE email=?", (case["sender"],))
        hs.upsert_contact(case["sender"], (contact or {}).get("name", ""), cust["name"])
        deal = hs.create_deal(f"{cust['name']} - PO {res.po_number or case['ref']}", total, "Order confirmed", case["sender"], case["ref"])
        db.execute("UPDATE orders SET hubspot_deal_id=? WHERE id=?", (deal, oid))
    except Exception as e:
        note = f"HubSpot sync failed: {type(e).__name__}"
        audit(db, "system", "hubspot_sync_failed", case["ref"], str(e)[:200], now_iso(now))
    db.execute("UPDATE orders SET sync_note=? WHERE id=?", (note, oid))

    order = one(db, "SELECT * FROM orders WHERE id=?", (oid,))
    comms.queue_email(db, case["id"], "confirmation", case["sender"], f"Order confirmed {so['number']} [{case['ref']}]",
                      comms.confirmation_text(case, order, lines, cust), at=now_iso(now))
    return {"bc_order_no": so["number"], "order_id": oid}


# ------------------------------------------------------------------ human actions
def apply_overrides(db, mid, overrides, actor="rep", now=None):
    """overrides: [{'code': 'MISSING_PO', 'line_no': None, 'value': 'NW-123'}, ...] then re-validate end to end."""
    case = one(db, "SELECT * FROM messages WHERE id=?", (mid,))
    cur = json.loads(case["overrides_json"])
    for o in overrides:
        cur.append({**o, "by": actor, "at": now_iso(now)})
        audit(db, actor, "override", case["ref"], json.dumps(o), now_iso(now))
    db.execute("UPDATE messages SET overrides_json=? WHERE id=?", (json.dumps(cur), mid))
    return process_case(db, mid, now=now, actor=actor)


def send_draft(db, email_id, actor="rep", now=None):
    e = one(db, "SELECT * FROM outbound_emails WHERE id=?", (email_id,))
    comms.send_queued(db, email_id, at=now_iso(now))
    case = one(db, "SELECT * FROM messages WHERE id=?", (e["message_id"],))
    if case["status"] == "NEEDS_REVIEW":
        _set_status(db, case["id"], "AWAITING_CUSTOMER", "Clarification sent", now)
    audit(db, actor, "send_clarification", case["ref"], f"email {email_id}", now_iso(now))
    db.commit()


def sync_shipments(db, now=None):
    """Poll the ERP for posted shipments, move cases to SHIPPED and tell the customer."""
    bc, hs, n = get_bc(db), get_hubspot(db), 0
    shipped = {s["number"]: s["tracking"] for s in bc.shipped_orders()}
    for o in q(db, "SELECT o.*, m.sender, m.ref, m.id mid, m.status FROM orders o JOIN messages m ON m.id=o.message_id WHERE m.status='CONFIRMED'"):
        if o["bc_order_no"] in shipped:
            _set_status(db, o["mid"], "SHIPPED", f"Tracking {shipped[o['bc_order_no']]}", now)
            case = one(db, "SELECT * FROM messages WHERE id=?", (o["mid"],))
            comms.queue_email(db, o["mid"], "shipped", o["sender"], f"Order {o['bc_order_no']} has shipped [{o['ref']}]",
                              comms.shipped_text(case, o, shipped[o["bc_order_no"]]), at=now_iso(now))
            if o["hubspot_deal_id"]:
                try:
                    hs.update_deal_stage(o["hubspot_deal_id"], "Shipped")
                except Exception:
                    pass
            n += 1
    db.commit()
    return n


# ------------------------------------------------------------------ read helpers (UI / API)
def case_status(db, ref, email):
    """Customer-facing lookup. Only returns data if the email matches the sender or a contact at the same customer."""
    c = one(db, "SELECT * FROM messages WHERE ref=?", (ref.strip().upper(),))
    if not c:
        return None
    email = (email or "").strip().lower()
    same_acct = one(db, "SELECT 1 x FROM contacts WHERE email=? AND customer_id=?", (email, c["customer_id"])) if c["customer_id"] else None
    if email != (c["sender"] or "").lower() and not same_acct:
        return None
    order = one(db, "SELECT * FROM orders WHERE message_id=?", (c["id"],))
    return {"ref": c["ref"], "status": c["status"], "po_number": c["po_number"], "order": order,
            "lines": q(db, "SELECT * FROM order_lines WHERE order_id=?", (order["id"],)) if order else [],
            "history": q(db, "SELECT status, note, at FROM status_history WHERE message_id=? ORDER BY id", (c["id"],))}
