"""Synthetic order generator with ground truth.

Produces realistic, deliberately messy inbound orders (.eml files incl. real PDF attachments, Magento-shaped JSON,
portal-form payloads) PLUS a truth file saying which exceptions each one *should* trigger. run_demo.py then measures
how well the pipeline actually did against that truth - nothing here is hand-picked to flatter the results.
"""
import io, json, random
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime

from .db import q, one
from .validate import fuzzy_candidates, norm

SINGLE = {"missing_po": 3, "unknown_sku": 3, "ambiguous_sku": 3, "missing_qty": 2, "below_moq": 2, "pack_multiple": 2,
          "price_mismatch": 3, "past_date": 2, "missing_ship_to": 2, "ship_to_unrecognized": 1, "credit_hold": 2,
          "credit_limit": 2, "unknown_customer": 3, "description_only": 2, "stock_shortage": 3}
CODE = {"missing_po": "MISSING_PO", "unknown_sku": "UNKNOWN_SKU", "ambiguous_sku": "AMBIGUOUS_SKU", "missing_qty": "MISSING_QTY",
        "below_moq": "BELOW_MOQ", "pack_multiple": "PACK_MULTIPLE", "price_mismatch": "PRICE_MISMATCH", "past_date": "PAST_DATE",
        "missing_ship_to": "MISSING_SHIP_TO", "ship_to_unrecognized": "SHIP_TO_UNRECOGNIZED", "credit_hold": "CREDIT_HOLD",
        "credit_limit": "CREDIT_LIMIT", "unknown_customer": "UNKNOWN_CUSTOMER", "description_only": "UNKNOWN_SKU",
        "stock_shortage": "INSUFFICIENT_STOCK", "duplicate_po": "DUPLICATE_PO"}
LINE_DEFECTS = {"unknown_sku", "ambiguous_sku", "missing_qty", "below_moq", "pack_multiple", "price_mismatch", "description_only", "stock_shortage"}
A_ONLY = {"missing_qty", "description_only"}
BC_ONLY = {"price_mismatch"}
COMBO_OK = ["missing_po", "unknown_sku", "ambiguous_sku", "below_moq", "pack_multiple", "price_mismatch", "past_date", "missing_ship_to", "missing_qty"]
HOLD, NEAR_LIMIT = (7, 19), (13, 25)
BASE = datetime(2026, 10, 12, 7, 0)


class World:
    def __init__(self, db, rng):
        self.db, self.rng = db, rng
        self.items = {i["sku"]: i for i in q(db, "SELECT i.*, v.on_hand - v.reserved AS avail FROM items i JOIN inventory v ON v.sku=i.sku")}
        self.customers = {c["id"]: c for c in q(db, "SELECT * FROM customers")}
        self.sites = {}
        for s in q(db, "SELECT * FROM ship_to"):
            self.sites.setdefault(s["customer_id"], []).append(s)
        self.contacts = {}
        for c in q(db, "SELECT * FROM contacts"):
            self.contacts.setdefault(c["customer_id"], []).append(c)
        self.xref = {}
        for x in q(db, "SELECT * FROM customer_xref"):
            self.xref.setdefault(x["customer_id"], {})[x["sku"]] = x["customer_part"]

    def price(self, cust, sku):
        return round(self.items[sku]["list_price"] * (1 - cust["discount_pct"]), 2)

    def valid_qty(self, it, cap):
        ks = [k for k in (1, 2, 3, 4, 5, 6, 8, 10) if it["moq"] <= k * it["pack_multiple"] <= cap]
        return it["pack_multiple"] * self.rng.choice(ks) if ks else None

    def clean_lines(self, cust, n, xref=False, budget=None):
        for _ in range(60):
            pool = list(self.xref.get(cust["id"], {})) if xref and self.xref.get(cust["id"]) else list(self.items)
            lines, used = [], set()
            for sku in self.rng.sample(pool, min(n, len(pool))):
                it = self.items[sku]
                qty = self.valid_qty(it, min(it["avail"], 80))
                if not qty or sku in used:
                    continue
                used.add(sku)
                part = self.xref[cust["id"]][sku] if xref and sku in self.xref.get(cust["id"], {}) else \
                    self.rng.choice([sku, sku, sku.lower(), sku.replace("-", "")])
                lines.append({"sku": sku, "part": part, "qty": qty, "quoted": self.price(cust, sku), "desc": it["description"], "expect": True})
            val = sum(l["qty"] * l["quoted"] for l in lines)
            if lines and (budget is None or val < budget):
                return lines
        raise RuntimeError("could not build clean lines")


def _date_text(rng, d):
    return rng.choice([d.isoformat(), d.strftime("%b %d, %Y").replace(" 0", " "), f"{d.month}/{d.day}/{d.year}", f"{d.day} {d.strftime('%b %Y')}"])


def _ship_text(rng, site):
    return rng.choice([site["code"], f"{site['address']}, {site['city']}, {site['state']} {site['zip']}", f"{site['city']} {site['state']} {site['zip']}"])


def _po(rng):
    return rng.choice([f"{rng.choice(['NW', 'CE', 'JB', 'XR'])}-{rng.randint(10000, 99999)}", str(rng.randint(4500000000, 4500999999))])


def make_spec(W, t, cust, style, n_lines, xref=False):
    rng = W.rng
    sites = W.sites[cust["id"]]
    need_ship = len(sites) > 1 and not cust["default_ship_code"]
    spec = {"cust": cust, "t": t, "style": style, "po": _po(rng), "need": (t + timedelta(days=rng.randint(8, 35))).date(),
            "ship": _ship_text(rng, rng.choice(sites)) if (need_ship or rng.random() < 0.5) else None,
            "sender": rng.choice(W.contacts[cust["id"]])["email"], "account_line": rng.random() < 0.3, "defects": [], "notes": []}
    if rng.random() < 0.15:                                       # a colleague we have no contact record for -> domain match
        spec["sender"] = f"purchasing@{cust['email_domain']}"
    spec["po_true"], spec["ship_code"] = spec["po"], sites[0]["code"] if not spec["ship"] else next(
        (x["code"] for x in sites if x["code"] in spec["ship"] or x["zip"] in spec["ship"] or x["city"] in spec["ship"]), sites[0]["code"])
    budget = 0.6 * (cust["credit_limit"] - cust["balance"])
    spec["lines"] = W.clean_lines(cust, n_lines, xref, budget)
    return spec


def apply_defect(W, spec, d, used_lines):
    rng, cust = W.rng, spec["cust"]
    spec["defects"].append(d)
    cands = [i for i in range(len(spec["lines"])) if i not in used_lines]
    if d in LINE_DEFECTS and not cands:                     # every line already carries a defect -> add a fresh clean one
        spec["lines"].append(W.clean_lines(cust, 1)[0])
        cands = [len(spec["lines"]) - 1]
    li = rng.choice(cands) if d in LINE_DEFECTS else None
    if li is not None:
        used_lines.add(li)
    L = spec["lines"][li] if li is not None else None
    if d == "missing_po":
        spec["po"] = None
    elif d == "unknown_sku":
        L.update(part="ZQ-9137-XX", expect=False)
    elif d == "ambiguous_sku":
        sku = L["sku"]
        for k in range(len(sku) - 1, 0, -1):
            if sku[k].isalnum():
                for rep in "5XQ7":
                    cand = sku[:k] + rep + sku[k + 1:]
                    c = fuzzy_candidates(W.db, cand, None, 1)
                    if norm(cand) not in {norm(s) for s in W.items} and c and c[0]["score"] >= 0.8:
                        L.update(part=cand, expect=False)
                        return
        raise RuntimeError("no ambiguous variant")
    elif d == "missing_qty":
        L.update(fix_qty=L["qty"], qty=None, expect=False)
    elif d == "description_only":
        L.update(part=None, qty=10, expect=False, text="10 pcs of 1/2 inch stainless elbow")
    elif d == "below_moq":
        big = [s for s, it in W.items.items() if it["moq"] >= 5 and it["avail"] >= 20]
        sku = rng.choice(big)
        L.update(sku=sku, part=sku, qty=max(1, W.items[sku]["moq"] - 3), quoted=W.price(cust, sku), expect=False, fix_qty=W.items[sku]["moq"])
    elif d == "pack_multiple":
        sk = [s for s, it in W.items.items() if it["pack_multiple"] > 1 and it["avail"] >= 40 and (it["moq"] + 1) % it["pack_multiple"]]
        sku = rng.choice(sk)
        pk = W.items[sku]["pack_multiple"]
        L.update(sku=sku, part=sku, qty=W.items[sku]["moq"] + 1, quoted=W.price(cust, sku), expect=False,
                 fix_qty=-(-(W.items[sku]["moq"] + 1) // pk) * pk)
    elif d == "price_mismatch":
        L.update(quoted=round(L["quoted"] * 0.85, 2), expect=False)
    elif d == "past_date":
        spec["need"] = (spec["t"] - timedelta(days=3)).date()
    elif d == "missing_ship_to":
        spec["ship"] = None
    elif d == "ship_to_unrecognized":
        spec["ship"] = "77 Unknown Blvd, Nowhere, ZZ 00000"
    elif d == "stock_shortage":
        tight = [s for s, it in W.items.items() if 0 < it["avail"] <= 60 and it["avail"] >= 1]
        sku = rng.choice(tight)
        it = W.items[sku]
        qty = (it["avail"] // it["pack_multiple"] + 2) * it["pack_multiple"]
        qty = max(qty, it["moq"])
        L.update(sku=sku, part=sku, qty=qty, quoted=W.price(cust, sku))
    spec.setdefault("expect_codes", set())


def trim_to_budget(spec):
    """Keep generated orders inside the customer's credit headroom so credit exceptions only appear when injected."""
    c = spec["cust"]
    limit = 0.8 * (c["credit_limit"] - c["balance"])
    val = lambda: sum((l["qty"] or 0) * l["quoted"] for l in spec["lines"])
    while val() > limit and len(spec["lines"]) > 1:
        victims = [l for l in spec["lines"] if l.get("expect", True)]
        if not victims:
            break
        spec["lines"].remove(victims[-1])


# ------------------------------------------------------------------ renderers
def _line_text(rng, L, style_variant):
    if L.get("text"):
        return L["text"]
    part, qty = L["part"], L["qty"]
    if qty is None:
        return f"- {part}"
    return rng.choice([f"{qty} x {part}", f"{qty} pcs {part}", f"{part} - {qty} pcs", f"Qty: {qty} Part: {part}", f"{part} x {qty}"])


def body_text(W, spec):
    rng, c = W.rng, spec["cust"]
    name = spec["sender"].split("@")[0].replace(".", " ").title().split(" ")[0]
    po, need, ship, lines, style = spec["po"], spec["need"], spec["ship"], spec["lines"], spec["style"]
    sig = f"\nThanks,\n{name}\n{spec.get('sig_company', c['name'])}" + (f"\nAccount: {c['account_no']}" if spec["account_line"] else "")
    po_lab = rng.choice(["PO#", "PO:", "Purchase Order:", "PO number:"])
    if style in ("A", "F", "E"):
        out = ["Hi team,", "", "Resending in case you missed it - please enter this order." if spec.get("resend") else "Please enter the following order."]
        if po:
            out.append(f"{po_lab} {po}")
        if need:
            out.append(rng.choice(["Need by", "Required by", "Deliver by"]) + f" {_date_text(rng, need)}")
        if ship:
            out.append(f"Ship to: {ship}")
        out.append("")
        out += [_line_text(rng, L, style) for L in lines]
        txt = "\n".join(out) + "\n" + sig
        if style == "F":
            txt += ("\n\n> On Mon, Oct 5, Northfield Fluid Systems wrote:\n> Quote Q-48213 attached: 10 x HF-0412-SS @ $6.72\n"
                    "\n-----Original Message-----\nFrom: orders@northfield.example\nPlease find 99 x OLD-1111-XX\n")
        return txt
    if style == "B":
        out = ["Hello,", "", f"Purchase Order: {po}" if po else "Please process this order:"]
        if need:
            out.append(f"Deliver by {_date_text(rng, need)}")
        if ship:
            out.append(f"Ship to: {ship}")
        out.append("")
        for L in lines:
            out.append(f"{L['part']} qty {L['qty']} @ ${L['quoted']:.2f}")
        return "\n".join(out) + "\n" + sig
    if style == "C":
        return f"Hi,\n\nPurchase order attached - please confirm.\n{sig}"
    if style == "D":
        items = [f"{L['qty']} of {L['part']}" for L in lines]
        listing = ", ".join(items[:-1]) + (" and " if len(items) > 1 else "") + items[-1]
        out = ["Hi,", "", f"Could you send {listing}?"]
        tail = []
        if need:
            tail.append(f"Need by {_date_text(rng, need)}.")
        if po:
            tail.append(f"PO {po}.")
        out.append(" ".join(tail))
        if ship:
            out.append(f"Ship to: {ship}")
        return "\n".join(out) + "\n" + sig
    raise ValueError(style)


def pdf_bytes(W, spec):
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    cv = canvas.Canvas(buf, pagesize=letter)
    y = 740
    rows = ["PURCHASE ORDER", spec.get("sig_company", spec["cust"]["name"]), ""]
    if spec["po"]:
        rows.append(f"PO Number: {spec['po']}")
    if spec["need"]:
        rows.append(f"Date needed: {_date_text(W.rng, spec['need'])}")
    if spec["ship"]:
        rows.append(f"Ship To: {spec['ship']}")
    rows += ["", "Line  Part No.  Description  Qty  UOM  Unit Price  Ext Price"]
    for i, L in enumerate(spec["lines"], 1):
        rows.append(f"{i}  {L['part']}  {L.get('desc', 'item').replace(chr(34), 'in')[:28]}  {L['qty']}  EA  ${L['quoted']:.2f}  ${L['qty'] * L['quoted']:.2f}")
    for r in rows:
        cv.drawString(50, y, r)
        y -= 16
    cv.save()
    return buf.getvalue()


def write_eml(W, spec, path):
    msg = EmailMessage()
    c = spec["cust"]
    msg["From"] = f"{spec['sender'].split('@')[0].title()} <{spec['sender']}>"
    msg["To"] = "orders@northfield.example"
    po_in_subj = f" {spec['po']}" if spec["po"] and W.rng.random() < 0.5 else ""
    msg["Subject"] = {"A": "New order", "B": "Order request", "C": "PO attached", "D": "Parts needed", "E": "Reorder", "F": "RE: order"}[spec["style"]] + po_in_subj
    msg["Date"] = format_datetime(spec["t"].replace(tzinfo=timezone.utc))
    msg.set_content(body_text(W, spec))
    if spec["style"] == "C":
        msg.add_attachment(pdf_bytes(W, spec), maintype="application", subtype="pdf", filename=f"PO_{c['account_no']}.pdf")
    path.write_bytes(bytes(msg))


# ------------------------------------------------------------------ top level
def expected_codes(spec):
    return sorted({CODE[d] for d in spec["defects"]})


def truth_entry(spec, kind, ref):
    lines = [[L["sku"], L["qty"]] for L in spec["lines"] if L.get("expect", True)]
    site = spec["cust"].get("_site_code")
    return {"ref": ref, "kind": kind, "po_true": spec.get("po_true") or spec["po"], "true_account": spec["cust"]["account_no"],
            "fix_need": (spec["t"] + timedelta(days=14)).date().isoformat(), "ship_true": spec.get("ship_code"),
            "lines_all": [{"sku": L["sku"], "fix_qty": L.get("fix_qty", L["qty"])} for L in spec["lines"]], "customer": spec["cust"]["account_no"] if "unknown_customer" not in spec["defects"] else None,
            "defects": spec["defects"], "expected_codes": expected_codes(spec), "expected_lines": lines,
            "expect_touchless": not any(CODE[d] != "INSUFFICIENT_STOCK" for d in spec["defects"])}


def generate(db, outdir, seed=7):
    rng = random.Random(seed)
    W = World(db, rng)
    outdir.mkdir(parents=True, exist_ok=True)
    for f in outdir.glob("*.eml"):
        f.unlink()
    normal = [c for cid, c in W.customers.items() if cid not in HOLD + NEAR_LIMIT]
    multi_nodef = [c for c in normal if len(W.sites[c["id"]]) > 1 and not c["default_ship_code"]]
    req_po = [c for c in normal if c["requires_po"]]
    xref_c = [c for c in normal if c["id"] in W.xref]
    events = []                                                    # (time, kind, payload)
    clock = iter(sorted(BASE + timedelta(minutes=rng.randint(0, 4 * 24 * 60 + 600)) for _ in range(200)))

    def new_spec(style, n=None, cust=None, xref=False):
        cust = cust or rng.choice(xref_c if xref else normal)
        return make_spec(W, next(clock), cust, style, n or rng.choice([1, 2, 2, 3, 4]), xref)

    clean_specs = []
    for style in ["A"] * 10 + ["B"] * 8 + ["C"] * 6 + ["D"] * 5 + ["E"] * 5 + ["F"] * 4:
        s = new_spec(style, xref=(style == "E"))
        clean_specs.append(s)
        events.append(("email", s, "clean"))

    for d, n in SINGLE.items():
        for _ in range(n):
            style = rng.choice(["A", "F"] if d in A_ONLY else ["B", "C"] if d in BC_ONLY else ["A", "B", "C", "D", "F"])
            cust = rng.choice(multi_nodef) if d == "missing_ship_to" else rng.choice(req_po) if d == "missing_po" else None
            if d in ("credit_hold", "credit_limit"):
                cid = rng.choice(HOLD if d == "credit_hold" else NEAR_LIMIT)
                cust = W.customers[cid]
                s = make_spec(W, next(clock), cust, style, 3)
                s["lines"] = W.clean_lines(cust, 3)
                if d == "credit_limit":
                    while sum(l["qty"] * l["quoted"] for l in s["lines"]) < 600:
                        s["lines"] = W.clean_lines(cust, 3)
                if len(W.sites[cid]) > 1 and not s["ship"]:
                    s["ship"] = W.sites[cid][0]["code"]
            else:
                s = new_spec(style, n=3 if d in LINE_DEFECTS else None, cust=cust)
                if d == "price_mismatch" and s["cust"]["discount_pct"] == 0:
                    pass                                           # quoted 15% under list is still a mismatch
            if d == "unknown_customer":
                s["sender"] = rng.choice(["buyer@newshop.example", "orders@gmail-like.example", "jsmith@unknown-co.example"])
                s["account_line"], s["defects"], s["sig_company"], s["style"] = False, ["unknown_customer"], "Walkin Supply Ltd", "A"
                s["ship"] = None
            else:
                apply_defect(W, s, d, set())
                if d not in ("credit_hold", "credit_limit"):
                    trim_to_budget(s)
            events.append(("email", s, "defect"))

    for _ in range(8):                                             # multi-defect orders
        pair = rng.sample(COMBO_OK, 2)
        while set(pair) & A_ONLY and set(pair) & BC_ONLY:
            pair = rng.sample(COMBO_OK, 2)
        style = "A" if set(pair) & A_ONLY else rng.choice(["B", "C"]) if set(pair) & BC_ONLY else rng.choice(["A", "B", "C", "D", "F"])
        cust = rng.choice(multi_nodef) if "missing_ship_to" in pair else rng.choice(req_po) if "missing_po" in pair else None
        s = new_spec(style, n=3, cust=cust)
        used = set()
        for d in pair:
            apply_defect(W, s, d, used)
        trim_to_budget(s)
        events.append(("email", s, "combo"))

    originals = rng.sample(clean_specs, 3)                         # duplicate POs: customer re-sends the same order later
    for o in originals:
        dup = dict(o, t=o["t"] + timedelta(hours=rng.randint(20, 40)), defects=["duplicate_po"], style="A", resend=True, lines=[dict(l, part=l["sku"]) for l in o["lines"]])
        events.append(("email", dup, "duplicate"))

    # ---- write emails in arrival order, build truth
    events.sort(key=lambda e: e[1]["t"])
    truth, files = [], []
    for i, (_, spec, kind) in enumerate(events, 1):
        path = outdir / f"{i:03d}_{kind}.eml"
        write_eml(W, spec, path)
        files.append((spec["t"], "eml", str(path)))
        t = truth_entry(spec, kind, None)
        t["file"] = path.name
        t["channel"] = "email"
        truth.append(t)

    # ---- magento orders
    mg = []
    mg_defects = [None, None, None, None, "missing_po", "price_mismatch", "price_mismatch", "below_moq", "stock_shortage", "unknown_customer"]
    for j, d in enumerate(mg_defects):
        cust = rng.choice([c for c in normal if c["discount_pct"] > 0] if d == "price_mismatch" else normal)
        s = make_spec(W, next(clock), cust, "A", 2)
        if d and d != "unknown_customer":
            apply_defect(W, s, d, set())
        if d == "price_mismatch":
            for L in s["lines"]:                                    # web store charged list price, not the contract price
                L["quoted"] = W.items[L["sku"]]["list_price"]
            s["lines"][0]["expect"] = False
        site = rng.choice(W.sites[cust["id"]])
        if d == "unknown_customer":
            s["sender"], s["defects"] = "walkin@webshopper.example", ["unknown_customer"]
        o = {"increment_id": f"MG-{100045 + j}", "created_at": s["t"].replace(microsecond=0).isoformat(), "customer_email": s["sender"],
             "payment": ({"po_number": s["po"]} if s["po"] else {}),
             "items": [{"sku": L["sku"], "qty_ordered": L["qty"], "price": L["quoted"]} for L in s["lines"] if L["qty"]],
             "extension_attributes": {"shipping_assignments": [{"shipping": {"address": {
                 "street": [site["address"]], "city": site["city"], "region_code": site["state"], "postcode": site["zip"]}}}]}}
        mg.append(o)
        t = truth_entry(s, "magento", None)
        t["channel"], t["file"] = "magento", o["increment_id"]
        truth.append(t)
    (outdir.parent / "magento_orders.json").write_text(json.dumps(mg, indent=1))

    # ---- portal form orders
    forms = []
    for d in [None, None, "missing_po", "below_moq", "pack_multiple", "past_date"]:
        cust = rng.choice(normal)
        s = make_spec(W, next(clock), cust, "A", 2)
        if d:
            apply_defect(W, s, d, set())
        if not s["ship"]:
            s["ship"] = W.sites[cust["id"]][0]["code"]
        else:
            s["ship"] = next((x["code"] for x in W.sites[cust["id"]]), s["ship"])
        forms.append({"sender": s["sender"], "po": s["po"], "date": s["need"].isoformat() if s["need"] else None, "ship": s["ship"],
                      "lines": [[L["sku"], L["qty"]] for L in s["lines"]], "t": s["t"].isoformat()})
        t = truth_entry(s, "form", None)
        t["channel"], t["file"] = "web_form", f"form_{len(forms)}"
        truth.append(t)
    (outdir.parent / "form_orders.json").write_text(json.dumps(forms, indent=1))
    (outdir.parent / "truth.json").write_text(json.dumps(truth, indent=1))
    return truth
