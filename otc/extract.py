"""Order extraction: unstructured email/PDF text -> structured Extracted.

Two interchangeable extractors:
  * rule_extract  - deterministic regex parser (always available, offline, free)
  * llm_extract   - Claude with a strict JSON contract; any failure falls back to the rule parser

The LLM only *reads* the message. Every business decision (price, credit, stock, MOQ...) is made later by
deterministic code in validate.py - the model never decides whether an order is allowed.
"""
import json, os, re
from datetime import date
from . import config
from .models import Extracted, Line

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"

URL_EMAIL = re.compile(r"\S+@\S+|https?://\S+")
PO_RE = re.compile(
    r"(?:\bP\.?\s?O\.?\b\s*(?:#|no\.?|number|num)?|\bpurchase\s+order\s*(?:#|no\.?|number)?)\s*[:#\-]?\s*"
    r"([A-Za-z0-9][A-Za-z0-9\-/]{2,})", re.I)
ACCT_RE = re.compile(r"\b(?:acct|account)\s*(?:#|no\.?|number)?\s*[:#]?\s*([A-Za-z]-?\d{3,6})\b", re.I)
REF_RE = re.compile(r"\b(?:quote|quotation|invoice|ref(?:erence)?|rfq)\s*(?:#|no\.?)?\s*[:#]?\s*[A-Za-z]{0,4}-?\d+\b", re.I)
PART_RE = re.compile(r"\b[A-Za-z]{1,6}-?\d{2,6}(?:-?[A-Za-z0-9]{1,5}){0,2}\b")
STOP_RE = re.compile(r"^\s*(?:-{2,}\s*(?:original|forwarded)|on .{5,80} wrote:|from:\s.*@|_{5,}|-- ?$)", re.I)
NEED_RE = re.compile(r"need(?:ed)?\s*by|deliver|required|requested|ship\s*by|\bdue\b|date\s+needed|by\s+date", re.I)

QTY_LABEL = re.compile(r"(?:qty|quantity)\.?\s*[:=]?\s*(\d[\d,]*)", re.I)
QTY_BEFORE_UNIT = re.compile(r"(\d[\d,]*)\s*(?:x|×|pcs?|pieces|units?|ea|each)\s*(?:of)?\s*[:\-]?\s*$", re.I)
QTY_BEFORE_OF = re.compile(r"(\d[\d,]*)\s*of\s*$", re.I)
QTY_AFTER_X = re.compile(r"^\s*[,:\-–]?\s*(?:x|×)\s*(\d[\d,]*)\b", re.I)
QTY_AFTER_UNIT = re.compile(r"(\d[\d,]*)\s*(?:pcs?|pieces|units?|ea|each)\b", re.I)
QTY_AFTER_PLAIN = re.compile(r"^\s*[,:\-–]?\s*(\d[\d,]*)(?![\d/\.\-])\b")
PRICE_AT = re.compile(r"@\s*\$?\s*(\d+(?:\.\d{1,4})?)")
PRICE_USD = re.compile(r"\$\s*(\d[\d,]*(?:\.\d{1,4})?)")
DESC_CLAUSE = re.compile(r"(\d[\d,]*)\s*(?:x|×|pcs?|pieces|units?|ea)\b\s*(?:of\s+)?(\S.{5,60}?)(?=[.,;]|$)", re.I)


def _iso(y, m, d):
    try:
        if y < 100:
            y += 2000
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def find_dates(text):
    """Return ISO dates found in text. US convention (m/d/yyyy) for numeric dates."""
    out = []
    for m in re.finditer(r"\b(20\d{2})-(\d{2})-(\d{2})\b", text):
        out.append((m.start(), _iso(int(m[1]), int(m[2]), int(m[3]))))
    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})/(20\d{2}|\d{2})\b", text):
        out.append((m.start(), _iso(int(m[3]), int(m[1]), int(m[2]))))
    for m in re.finditer(r"\b" + _MON + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})\b", text, re.I):
        out.append((m.start(), _iso(int(m[3]), MONTHS[m[1].lower()], int(m[2]))))
    for m in re.finditer(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + _MON + r",?\s+(20\d{2})\b", text, re.I):
        out.append((m.start(), _iso(int(m[3]), MONTHS[m[2].lower()], int(m[1]))))
    return sorted((p, d) for p, d in out if d)


def _clean_body(body: str) -> str:
    """Drop quoted replies, forwarded chains and signatures so old order lines are never re-read."""
    keep = []
    for ln in body.splitlines():
        if STOP_RE.match(ln):
            break
        if ln.lstrip().startswith(">"):
            continue
        keep.append(ln)
    return "\n".join(keep)


def _find_po(text):
    for m in PO_RE.finditer(text):
        tok = m.group(1)
        if any(c.isdigit() for c in tok):
            return tok.strip("-/")
    return None


def _strip_refs(s: str) -> str:
    s = URL_EMAIL.sub(" ", s)
    s = PO_RE.sub(lambda m: " " if any(c.isdigit() for c in m.group(1)) else m.group(0), s)
    s = ACCT_RE.sub(" ", s)
    s = REF_RE.sub(" ", s)
    return s


def _to_int(s):
    try:
        v = int(s.replace(",", ""))
        return v if 0 < v < 1_000_000 else None
    except ValueError:
        return None


def _qty_before(win):
    for rx in (QTY_LABEL,):
        ms = list(rx.finditer(win))
        if ms:
            return _to_int(ms[-1].group(1))
    for rx in (QTY_BEFORE_UNIT, QTY_BEFORE_OF):
        m = rx.search(win)
        if m:
            return _to_int(m.group(1))
    return None


def _qty_after(win):
    clean = PRICE_AT.sub(" ", PRICE_USD.sub(" ", win))
    for rx in (QTY_LABEL, QTY_AFTER_X, QTY_AFTER_UNIT, QTY_AFTER_PLAIN):
        m = rx.search(clean)
        if m:
            return _to_int(m.group(1))
    return None


def _price(win):
    m = PRICE_AT.search(win) or PRICE_USD.search(win)
    return float(m.group(1).replace(",", "")) if m else None


def parse_lines(body: str, skip=frozenset()):
    """Turn free text into order lines. Lines that cannot be tied to a part number (description-only clauses)
    are still emitted with part=None so they surface as exceptions instead of being silently dropped."""
    lines, n = [], 0
    for idx, raw in enumerate(body.splitlines()):
        if idx in skip:
            continue
        s = _strip_refs(raw)
        if not s.strip():
            continue
        s = re.sub(r"^\s*\d{1,3}[\.\)]?\s{2,}(?=\S)", "", s)       # leading table row number
        toks = [t for t in PART_RE.finditer(s) if not re.fullmatch(r"[xX]\d+", t.group(0))]   # "x50" is a quantity, not a part
        for i, t in enumerate(toks):
            prev_end = toks[i - 1].end() if i else 0
            next_start = toks[i + 1].start() if i + 1 < len(toks) else len(s)
            before, after = s[prev_end:t.start()], s[t.end():next_start]
            qty = _qty_before(before)
            if qty is None:
                qty = _qty_after(after)
            n += 1
            lines.append(Line(n, raw.strip(), t.group(0), qty, _price(after)))
        for m in DESC_CLAUSE.finditer(s):
            desc = m.group(2).strip()
            if PART_RE.search(desc) or not re.search(r"[A-Za-z]{3,}", desc) or (len(desc.split()) < 2 and len(desc) < 10):
                continue
            if _to_int(m.group(1)):
                n += 1
                lines.append(Line(n, raw.strip(), None, _to_int(m.group(1)), None, desc))
    return lines


def _ship_to(text):
    """Return (ship_to_text, set_of_line_indexes_consumed)."""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        m = re.search(r"ship(?:ping)?[\s\-]*to\s*[:\-]?\s*(.*)", ln, re.I)
        if m:
            chunk, used = ([m.group(1).strip()] if m.group(1).strip() else []), {i}
            for j in range(i + 1, min(i + 3, len(lines))):
                nxt = lines[j]
                if nxt.strip() and re.search(r"\d|,", nxt) and not PART_RE.search(nxt) and not re.search(r"\bx\b|\bqty\b|@", nxt, re.I):
                    chunk.append(nxt.strip())
                    used.add(j)
                else:
                    break
            return (" ".join(chunk) or None), used
    return None, set()


def rule_extract(sender: str, subject: str, body: str) -> Extracted:
    body = _clean_body(body)
    full = f"{subject}\n{body}"
    req = None
    for ln in full.splitlines():
        if NEED_RE.search(ln):
            ds = find_dates(ln)
            if ds:
                req = ds[0][1]
                break
    acct = ACCT_RE.search(full)
    ship_text, used = _ship_to(body)
    return Extracted(
        lines=parse_lines(body, used), po_number=_find_po(full), requested_date=req, ship_to_text=ship_text,
        customer_hint={"email": sender, "account_no": acct.group(1).upper() if acct else None}, extractor="rules")


# ------------------------------------------------------------------ LLM mode
LLM_SYSTEM = """You extract purchase-order data from customer emails for an industrial manufacturer.
Return ONLY a JSON object, no prose, no markdown fences, with exactly these keys:
{"po_number": string|null, "requested_date": "YYYY-MM-DD"|null, "ship_to_text": string|null,
 "account_no": string|null,
 "lines": [{"part": string|null, "qty": integer|null, "price": number|null, "description": string|null, "raw": string}]}
Rules: never invent values - use null when the message does not state them. 'part' is the part/SKU number exactly as
written by the customer (including customer-specific part numbers). If a line gives only a description, set part to null
and put the words in description. Ignore quoted earlier messages, signatures and legal footers. Dates: US convention for
numeric dates. Do not decide whether the order is valid; only transcribe it."""


def llm_extract(sender: str, subject: str, body: str) -> Extracted:
    import anthropic
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=config.LLM_MODEL, max_tokens=1500, system=LLM_SYSTEM,
        messages=[{"role": "user", "content": f"From: {sender}\nSubject: {subject}\n\n{body}"}])
    txt = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    txt = re.sub(r"^```(?:json)?|```$", "", txt, flags=re.M).strip()
    d = json.loads(txt)
    lines = []
    for i, l in enumerate(d.get("lines") or [], 1):
        qty = l.get("qty")
        lines.append(Line(i, str(l.get("raw") or ""), l.get("part"), int(qty) if qty is not None else None,
                          float(l["price"]) if l.get("price") is not None else None, l.get("description")))
    rd = d.get("requested_date")
    if rd and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", rd):
        rd = None
    return Extracted(lines=lines, po_number=d.get("po_number"), requested_date=rd, ship_to_text=d.get("ship_to_text"),
                     customer_hint={"email": sender, "account_no": d.get("account_no")}, extractor="llm")


def extract(sender: str, subject: str, body: str, mode: str = None) -> Extracted:
    mode = mode or config.EXTRACTOR_MODE
    use_llm = mode == "llm" or (mode == "auto" and os.getenv("ANTHROPIC_API_KEY"))
    if use_llm:
        try:
            return llm_extract(sender, subject, body)
        except Exception as e:                       # network, bad JSON, quota... -> never lose the order
            ex = rule_extract(sender, subject, body)
            ex.extractor, ex.notes = "rules(fallback)", f"LLM extraction failed: {type(e).__name__}"
            return ex
    return rule_extract(sender, subject, body)
