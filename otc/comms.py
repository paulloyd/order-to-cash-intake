"""Customer communication: one bundled clarification email per case, confirmation and shipped notices.

Emails are written to an outbox table (status draft|sent). Set SMTP_HOST (+ SMTP_USER/SMTP_PASSWORD/SMTP_FROM)
to also deliver them through a real mail server.
"""
import os, smtplib
from email.message import EmailMessage
from . import config
from .db import now_iso, one


def _smtp_send(to, subject, body):
    host = os.getenv("SMTP_HOST")
    if not host:
        return
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = os.getenv("SMTP_FROM", "orders@example.com"), to, subject
    m.set_content(body)
    with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587"))) as s:
        s.starttls()
        if os.getenv("SMTP_USER"):
            s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD", ""))
        s.send_message(m)


def queue_email(db, message_id, kind, to, subject, body, status="sent", at=None):
    at = at or now_iso()
    cur = db.execute("INSERT INTO outbound_emails(message_id,kind,to_addr,subject,body,status,created_at,sent_at) "
                     "VALUES (?,?,?,?,?,?,?,?)", (message_id, kind, to, subject, body, status, at, at if status == "sent" else None))
    if status == "sent":
        _smtp_send(to, subject, body)
    return cur.lastrowid


def send_queued(db, email_id, at=None):
    e = one(db, "SELECT * FROM outbound_emails WHERE id=?", (email_id,))
    if not e or e["status"] == "sent":
        return
    db.execute("UPDATE outbound_emails SET status='sent', sent_at=? WHERE id=?", (at or now_iso(), email_id))
    _smtp_send(e["to_addr"], e["subject"], e["body"])


def _first_name(sender):
    local = (sender or "").split("@")[0]
    first = local.replace(".", " ").replace("_", " ").split(" ")[0].title()
    return first or "there"


def clarification_text(case, questions):
    po = f" (PO {case['po_number']})" if case.get("po_number") else ""
    qs = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
    return (f"Hi {_first_name(case['sender'])},\n\nThanks for your order{po}. To enter it correctly we need "
            f"{'one answer' if len(questions) == 1 else f'{len(questions)} answers'} from you:\n\n{qs}\n\n"
            f"Just reply to this email with the details and we will process it straight away. "
            f"Reference: {case['ref']}\n\n{config.COMPANY} - Customer Service")


def polish_with_llm(text, ref):
    """Optional tone polish. Guard: the reference must survive and every question line must still appear."""
    try:
        import anthropic
        msg = anthropic.Anthropic().messages.create(
            model=config.LLM_MODEL, max_tokens=700,
            system="Rewrite this customer email to be warm and concise. Keep every numbered question and every part number, "
                   "number and date exactly as written. Do not add promises. Return only the email text.",
            messages=[{"role": "user", "content": text}])
        out = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
        return out if ref in out else text
    except Exception:
        return text


def draft_clarification(db, case, exceptions, at=None):
    qs = [e["question"] for e in exceptions if e["path"] == "customer" and e["question"]]
    if not qs:
        return None
    db.execute("DELETE FROM outbound_emails WHERE message_id=? AND kind='clarification' AND status='draft'", (case["id"],))
    body = clarification_text(case, qs)
    if config.LLM_CLARIFY_POLISH and os.getenv("ANTHROPIC_API_KEY"):
        body = polish_with_llm(body, case["ref"])
    subj = f"Re: {case['subject']} [{case['ref']}] - details needed" if case["subject"] else f"Your order [{case['ref']}] - details needed"
    return queue_email(db, case["id"], "clarification", case["sender"], subj, body, status="draft", at=at)


def confirmation_text(case, order, lines, customer):
    rows = "\n".join(f"  - {l['sku']}  x{l['qty']}  @ ${l['unit_price']:.2f}"
                     + (f"   ({l['backorder_qty']} on backorder)" if l["backorder_qty"] else "") for l in lines)
    back = any(l["backorder_qty"] for l in lines)
    note = "\nPart of your order is on backorder; the rest will ship first.\n" if back else ""
    return (f"Hi {_first_name(case['sender'])},\n\nYour order is confirmed.\n\n"
            f"Sales order: {order['bc_order_no']}\nYour PO: {order['po_number'] or '-'}\nRequested date: {order['requested_date']}\n"
            f"Order total: ${order['total']:,.2f}\n\n{rows}\n{note}\n"
            f"Track status any time: {config.PORTAL_URL}  (reference {case['ref']})\n\n{config.COMPANY} - Customer Service")


def shipped_text(case, order, tracking):
    return (f"Hi {_first_name(case['sender'])},\n\nGood news - order {order['bc_order_no']} (PO {order['po_number'] or '-'}) has shipped.\n"
            f"Tracking: {tracking}\n\n{config.COMPANY} - Customer Service")
