"""Deterministic validation. No LLM here - every rule is auditable code.

validate() takes the extracted order plus any human overrides and returns the resolved order and a list of
exceptions. An order is 'touchless' only when there are zero blocking exceptions.

Exception paths:
  customer  -> only the customer can answer (bundled into ONE clarification email)
  internal  -> a rep/approver can decide (price variance, credit, near-miss part number, duplicate)
  info      -> warning only (e.g. partial stock) - never blocks
"""
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from difflib import SequenceMatcher
from . import config
from .db import q, one
from .models import Exc, Extracted


def norm(s) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


@dataclass
class Result:
    customer: dict = None
    po_number: str = None
    requested_date: str = None
    ship_to: dict = None
    lines: list = field(default_factory=list)       # resolved lines
    exceptions: list = field(default_factory=list)
    order_value: float = 0.0
    discard: bool = False

    @property
    def blocking(self):
        return any(e.severity == "block" for e in self.exceptions)


def _ov(overrides, code, line_no=None):
    """Latest override for a code (and line, when line-specific)."""
    hit = None
    for o in overrides:
        if o["code"] == code and (line_no is None or o.get("line_no") in (None, line_no)):
            hit = o
    return hit


def resolve_customer(db, ex: Extracted, raw_text: str, overrides):
    o = _ov(overrides, "UNKNOWN_CUSTOMER")
    if o:
        c = one(db, "SELECT * FROM customers WHERE account_no=?", (str(o["value"]).strip().upper(),))
        if c:
            return c
    email = (ex.customer_hint.get("email") or "").lower().strip()
    if email:
        r = one(db, "SELECT c.* FROM contacts t JOIN customers c ON c.id=t.customer_id WHERE t.email=?", (email,))
        if r:
            return r
        dom = email.split("@")[-1]
        r = one(db, "SELECT * FROM customers WHERE email_domain=?", (dom,))
        if r:
            return r
    acct = ex.customer_hint.get("account_no")
    if acct:
        r = one(db, "SELECT * FROM customers WHERE account_no=?", (acct.upper(),))
        if r:
            return r
    low = (raw_text or "").lower()
    for c in q(db, "SELECT * FROM customers"):
        if c["name"].lower() in low:
            return c
    return None


def fuzzy_candidates(db, part, customer_id, limit=3):
    n = norm(part)
    pool = [(norm(i["sku"]), i["sku"], i["description"]) for i in q(db, "SELECT sku, description FROM items")]
    if customer_id:
        for x in q(db, "SELECT x.customer_part, x.sku, i.description FROM customer_xref x JOIN items i ON i.sku=x.sku "
                       "WHERE x.customer_id=?", (customer_id,)):
            pool.append((norm(x["customer_part"]), x["sku"], x["description"]))
    scored = sorted(((SequenceMatcher(None, n, k).ratio(), sku, d) for k, sku, d in pool), reverse=True)
    seen, out = set(), []
    for r, sku, d in scored:
        if sku not in seen:
            seen.add(sku)
            out.append({"sku": sku, "description": d, "score": round(r, 3)})
    return out[:limit]


def resolve_sku(db, part, customer_id):
    """Exact (normalized) SKU, else customer cross-reference. Never guesses."""
    n = norm(part)
    if not n:
        return None, None
    for i in q(db, "SELECT * FROM items"):
        if norm(i["sku"]) == n:
            return i, "exact"
    if customer_id:
        for x in q(db, "SELECT * FROM customer_xref WHERE customer_id=?", (customer_id,)):
            if norm(x["customer_part"]) == n:
                return one(db, "SELECT * FROM items WHERE sku=?", (x["sku"],)), "xref"
    return None, None


def match_ship_to(text, sites):
    t = (text or "").upper()
    for s in sites:
        if re.search(r"\b" + re.escape(s["code"]) + r"\b", t):
            return s
    zips = re.findall(r"\b\d{5}\b", t)
    hits = [s for s in sites if s["zip"] in zips]
    if len(hits) == 1:
        return hits[0]
    hits = [s for s in sites if s["city"].upper() in t]
    return hits[0] if len(hits) == 1 else None


def validate(db, ex: Extracted, raw_text="", overrides=(), received_date=None, message_id=None) -> Result:
    overrides = list(overrides)
    R = Result()
    received = received_date or date.today()
    cust = resolve_customer(db, ex, raw_text, overrides)
    R.customer = cust
    cid = cust["id"] if cust else None
    add = R.exceptions.append

    if not cust:
        add(Exc("UNKNOWN_CUSTOMER", "block", "customer",
                "Sender does not match a customer account.",
                "Could you confirm your company name or account number so we can match your order?",
                fix={"type": "text", "label": "Account number (e.g. C-1007)"}))

    # ---- PO number
    po = (_ov(overrides, "MISSING_PO") or {}).get("value") or ex.po_number
    R.po_number = po
    if not po and (cust is None or cust["requires_po"]):
        add(Exc("MISSING_PO", "block", "customer", "No purchase order number found.",
                "Please send the purchase order number for this order.", fix={"type": "text", "label": "PO number"}))

    # ---- duplicate PO (only meaningful once the customer is known)
    if po and cust:
        dup = one(db, "SELECT ref FROM messages WHERE customer_id=? AND po_number=? AND id<>? AND status IN "
                      "('CONFIRMED','SHIPPED','NEEDS_REVIEW','AWAITING_CUSTOMER') ORDER BY id LIMIT 1",
                  (cid, po, message_id or -1))
        o = _ov(overrides, "DUPLICATE_PO")
        if o and o["value"] == "discard":
            R.discard = True
        elif dup and not (o and o["value"] == "allow"):
            add(Exc("DUPLICATE_PO", "block", "internal", f"PO {po} was already received from this customer ({dup['ref']}).",
                    fix={"type": "select", "options": ["discard", "allow"], "label": "Duplicate - discard or allow?"}))

    # ---- requested date
    rd = (_ov(overrides, "PAST_DATE") or {}).get("value") or ex.requested_date
    if rd and rd < received.isoformat():
        add(Exc("PAST_DATE", "block", "customer", f"Requested date {rd} is before the order was received.",
                "The requested delivery date appears to be in the past - what date do you need this by?",
                fix={"type": "text", "label": "Need-by date (YYYY-MM-DD)"}))
    R.requested_date = rd or (received + timedelta(days=config.DEFAULT_LEAD_DAYS)).isoformat()

    # ---- lines
    disc = (cust["discount_pct"] if cust else 0) or 0
    for ln in ex.lines:
        row = {"line_no": ln.line_no, "raw": ln.raw, "sku": None, "description": None, "qty": ln.qty, "unit_price": None,
               "available": None, "backorder_qty": 0, "match": None}
        R.lines.append(row)
        ovs = _ov(overrides, "UNKNOWN_SKU", ln.line_no) or _ov(overrides, "AMBIGUOUS_SKU", ln.line_no)
        item, how = (None, None)
        if ovs:
            item = one(db, "SELECT * FROM items WHERE sku=?", (str(ovs["value"]).strip().upper(),))
            how = "manual"
        if not item:
            item, how = resolve_sku(db, ln.part, cid)
        if not item:
            cands = fuzzy_candidates(db, ln.part, cid) if ln.part else []
            if cands and cands[0]["score"] >= config.FUZZY_THRESHOLD:
                opts = [c["sku"] for c in cands]
                add(Exc("AMBIGUOUS_SKU", "block", "internal",
                        f"Line {ln.line_no}: '{ln.part}' is not a valid part number. Closest: "
                        + ", ".join(f"{c['sku']} ({c['score']:.0%})" for c in cands),
                        line_no=ln.line_no, fix={"type": "select", "options": opts, "label": "Correct part number"}))
            else:
                what = ln.part or (ln.description or ln.raw)
                add(Exc("UNKNOWN_SKU", "block", "customer", f"Line {ln.line_no}: could not identify '{what}'.",
                        f"Which part number do you need for line {ln.line_no} ('{what}')?", line_no=ln.line_no,
                        fix={"type": "text", "label": "Part number"}))
            continue
        row.update(sku=item["sku"], description=item["description"], match=how)

        qo = _ov(overrides, "MISSING_QTY", ln.line_no) or _ov(overrides, "BELOW_MOQ", ln.line_no) or \
            _ov(overrides, "PACK_MULTIPLE", ln.line_no)
        qty = int(qo["value"]) if qo else ln.qty
        row["qty"] = qty
        if not qty or qty <= 0:
            add(Exc("MISSING_QTY", "block", "customer", f"Line {ln.line_no}: no quantity for {item['sku']}.",
                    f"How many {item['sku']} ({item['description']}) do you need?", line_no=ln.line_no,
                    fix={"type": "number", "label": "Quantity"}))
            continue
        if qty < item["moq"]:
            add(Exc("BELOW_MOQ", "block", "customer", f"Line {ln.line_no}: {qty} is below the minimum order of {item['moq']} for {item['sku']}.",
                    f"{item['sku']} has a minimum order quantity of {item['moq']}. May we enter {item['moq']} instead of {qty}?",
                    line_no=ln.line_no, fix={"type": "number", "label": "Quantity", "default": item["moq"]}))
        elif qty % item["pack_multiple"]:
            up = -(-qty // item["pack_multiple"]) * item["pack_multiple"]
            add(Exc("PACK_MULTIPLE", "block", "customer", f"Line {ln.line_no}: {item['sku']} ships in multiples of {item['pack_multiple']}; {qty} is not.",
                    f"{item['sku']} ships in multiples of {item['pack_multiple']}. May we enter {up} instead of {qty}?",
                    line_no=ln.line_no, fix={"type": "number", "label": "Quantity", "default": up}))

        contract = round(item["list_price"] * (1 - disc), 2)
        row["unit_price"] = contract
        if cust and ln.price is not None and abs(ln.price - contract) / contract > config.PRICE_TOLERANCE:
            o = _ov(overrides, "PRICE_MISMATCH", ln.line_no)
            if o and o["value"] == "quoted":
                row["unit_price"] = ln.price
            elif not o:
                add(Exc("PRICE_MISMATCH", "block", "internal",
                        f"Line {ln.line_no}: customer quoted ${ln.price:.2f}, contract price for {item['sku']} is ${contract:.2f}.",
                        line_no=ln.line_no, fix={"type": "select", "options": ["contract", "quoted"],
                                                 "label": "Which price applies? ('quoted' = approve variance)"}))

        inv = one(db, "SELECT on_hand - reserved AS av FROM inventory WHERE sku=?", (item["sku"],)) or {"av": 0}
        row["available"] = inv["av"]
        if qty > inv["av"]:
            row["backorder_qty"] = qty - max(inv["av"], 0)
            add(Exc("INSUFFICIENT_STOCK", "warn", "info",
                    f"Line {ln.line_no}: {item['sku']} has {max(inv['av'], 0)} available vs {qty} ordered - {row['backorder_qty']} will backorder.",
                    line_no=ln.line_no))

    R.order_value = round(sum((l["unit_price"] or 0) * (l["qty"] or 0) for l in R.lines), 2)

    # ---- credit
    if cust:
        approved = _ov(overrides, "CREDIT_HOLD") or _ov(overrides, "CREDIT_LIMIT")
        ok = bool(approved and str(approved["value"]).lower().startswith("approved"))
        if cust["credit_hold"]:
            if not ok:
                add(Exc("CREDIT_HOLD", "block", "internal", f"{cust['name']} is on credit hold.",
                        fix={"type": "bool", "label": "Finance approved release"}))
        elif cust["balance"] + R.order_value > cust["credit_limit"] and not ok:
            add(Exc("CREDIT_LIMIT", "block", "internal",
                    f"Order ${R.order_value:,.2f} + open balance ${cust['balance']:,.2f} exceeds credit limit ${cust['credit_limit']:,.2f}.",
                    fix={"type": "bool", "label": "Finance approved"}))

        # ---- ship-to
        sites = q(db, "SELECT * FROM ship_to WHERE customer_id=?", (cid,))
        o = _ov(overrides, "MISSING_SHIP_TO") or _ov(overrides, "SHIP_TO_UNRECOGNIZED")
        opts = ", ".join(f"{s['code']}: {s['address']}, {s['city']} {s['zip']}" for s in sites)
        if o:
            R.ship_to = next((s for s in sites if s["code"] == str(o["value"]).upper()), None)
        elif ex.ship_to_text:
            R.ship_to = match_ship_to(ex.ship_to_text, sites)
            if not R.ship_to:
                add(Exc("SHIP_TO_UNRECOGNIZED", "block", "customer", f"Ship-to '{ex.ship_to_text}' is not on file.",
                        f"We could not match that ship-to address to your account. On file: {opts}. Which should we use?",
                        fix={"type": "select", "options": [s["code"] for s in sites], "label": "Ship-to code"}))
        elif cust["default_ship_code"]:
            R.ship_to = next((s for s in sites if s["code"] == cust["default_ship_code"]), None)
        elif len(sites) == 1:
            R.ship_to = sites[0]
        else:
            add(Exc("MISSING_SHIP_TO", "block", "customer", "No ship-to given and the account has several sites.",
                    f"Which ship-to should we use? On file: {opts}.",
                    fix={"type": "select", "options": [s["code"] for s in sites], "label": "Ship-to code"}))

    if not R.lines:
        add(Exc("NO_LINES", "block", "customer", "No order lines found in the message.",
                "We could not find any items in your message - could you list the part numbers and quantities?",
                fix={"type": "text", "label": "n/a"}))
    return R
