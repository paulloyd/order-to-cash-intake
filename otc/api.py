"""Thin HTTP layer so n8n / Power Automate / Zapier can drive the pipeline.  Run:  uvicorn otc.api:app --port 8000"""
import base64
from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import db as D, pipeline, metrics

app = FastAPI(title="Order-to-Cash Intake", version="1.0")


class Attachment(BaseModel):
    filename: str
    content_base64: str


class EmailIn(BaseModel):
    sender: str
    subject: str = ""
    body: str = ""
    received_at: Optional[datetime] = None
    attachments: List[Attachment] = []


class FormIn(BaseModel):
    sender: str
    po_number: Optional[str] = None
    requested_date: Optional[str] = None
    ship_to_code: Optional[str] = None
    lines: List[List] = []          # [[sku, qty], ...]


class Override(BaseModel):
    code: str
    value: str
    line_no: Optional[int] = None


def _summary(db, mid):
    c = D.one(db, "SELECT ref,status,po_number,touchless FROM messages WHERE id=?", (mid,))
    c["exceptions"] = [{"code": e["code"], "severity": e["severity"], "path": e["path"], "message": e["message"]}
                       for e in D.q(db, "SELECT * FROM exceptions WHERE message_id=? AND status='open'", (mid,))]
    c["notify_rep"] = c["status"] == "NEEDS_REVIEW"
    d = D.one(db, "SELECT subject, body FROM outbound_emails WHERE message_id=? AND kind='clarification' AND status='draft' ORDER BY id DESC", (mid,))
    c["clarification_draft"] = d
    return c


@app.post("/intake/email")
def intake_email(m: EmailIn):
    db = D.connect()
    atts = [(a.filename, base64.b64decode(a.content_base64)) for a in m.attachments]
    mid = pipeline.ingest_email(db, m.sender, m.subject, m.body, atts, m.received_at)
    return _summary(db, mid)


@app.post("/intake/form")
def intake_form(f: FormIn):
    db = D.connect()
    mid = pipeline.ingest_form(db, f.sender, f.po_number, f.requested_date, f.ship_to_code, [(s, int(q)) for s, q in f.lines])
    return _summary(db, mid)


@app.post("/magento/pull")
def magento_pull():
    db = D.connect()
    return {"imported": [_summary(db, i) for i in pipeline.pull_magento(db)]}


@app.post("/shipments/sync")
def shipments_sync():
    return {"shipped": pipeline.sync_shipments(D.connect())}


@app.post("/cases/{ref}/resolve")
def resolve(ref: str, overrides: List[Override], actor: str = "rep"):
    db = D.connect()
    c = D.one(db, "SELECT id FROM messages WHERE ref=?", (ref,))
    if not c:
        raise HTTPException(404, "unknown reference")
    pipeline.apply_overrides(db, c["id"], [o.model_dump() for o in overrides], actor)
    return _summary(db, c["id"])


@app.get("/cases/{ref}")
def status(ref: str, email: str):
    s = pipeline.case_status(D.connect(), ref, email)
    if not s:
        raise HTTPException(404, "not found")
    return s


@app.get("/metrics")
def get_metrics():
    return metrics.kpis(D.connect())
