"""Rep dashboard + customer portal.   streamlit run app/streamlit_app.py"""
import json, sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pandas as pd
import streamlit as st

from otc import config, db as D, metrics, pipeline

st.set_page_config(page_title="Order-to-Cash Intake", page_icon="📦", layout="wide")
db = D.connect()
has_data = D.one(db, "SELECT COUNT(*) n FROM messages")["n"] > 0

st.sidebar.title(f"📦 {config.COMPANY}")
page = st.sidebar.radio("View", ["Dashboard", "Exception queue", "Place an order (portal)", "Order status (portal)", "Systems & outbox"])
st.sidebar.caption("Demo data is synthetic. ERP, CRM and web-store are simulated unless live mode is configured.")
if st.sidebar.button("Reset & load demo data"):
    import run_demo
    with st.spinner("Generating and processing ~100 synthetic orders..."):
        run_demo.main(quiet=True)
    st.rerun()
if not has_data and page in ("Dashboard", "Exception queue", "Systems & outbox"):
    st.info("No data yet. Click **Reset & load demo data** in the sidebar (or run `python run_demo.py`).")
    st.stop()


def status_badge(s):
    return {"CONFIRMED": "🟢", "SHIPPED": "🚚", "NEEDS_REVIEW": "🟠", "AWAITING_CUSTOMER": "🟡", "DISCARDED": "⚪"}.get(s, "⚫") + " " + s


# ---------------------------------------------------------------- Dashboard
if page == "Dashboard":
    st.title("Order intake performance")
    with st.sidebar.expander("Modeled assumptions (editable)"):
        a = {k: st.number_input(k.replace("_", " "), value=float(v), step=0.5) for k, v in config.ASSUMPTIONS.items()}
    k = metrics.kpis(db, a)
    c = st.columns(5)
    c[0].metric("Cases", k["cases"])
    c[1].metric("Touchless", f"{k['touchless_rate']:.0%}", f"{k['touchless']} orders")
    c[2].metric("Confirmed after review", k["assisted_confirmed"])
    c[3].metric("Open now", k["open"])
    c[4].metric("Avg pipeline time", f"{k['avg_processing_ms_first_pass']} ms")
    c = st.columns(4)
    c[0].metric("Customer emails / case", k["customer_emails_per_case"])
    c[1].metric("Questions per clarification email", k["avg_questions_per_clarification_email"])
    c[2].metric("Confirmed order value", f"${k['confirmed_order_value']:,.0f}")
    c[3].metric("Shipped (ERP sync)", k["shipped"])
    st.caption("All of the above are **measured** from the synthetic run.")
    st.subheader("Modeled rep time (depends on the assumptions in the sidebar - not a measurement)")
    m = st.columns(3)
    m[0].metric("Manual baseline (min)", k["MODELED_rep_minutes_manual"])
    m[1].metric("With automation (min)", k["MODELED_rep_minutes_with_automation"])
    m[2].metric("Saved (min)", k["MODELED_rep_minutes_saved"])
    l, r = st.columns(2)
    with l:
        st.subheader("Exceptions by reason (first pass)")
        df = pd.DataFrame(k["exceptions_first_pass"])
        st.bar_chart(df.set_index("code")["count"])
    with r:
        st.subheader("Cases by status")
        st.bar_chart(pd.Series(k["by_status"]))
    st.subheader("By channel")
    st.dataframe(pd.DataFrame(D.q(db, "SELECT channel, COUNT(*) cases, SUM(touchless) touchless FROM messages GROUP BY channel")), hide_index=True)

# ---------------------------------------------------------------- Exception queue
elif page == "Exception queue":
    st.title("Exception queue")
    cases = D.q(db, "SELECT m.*, c.name customer FROM messages m LEFT JOIN customers c ON c.id=m.customer_id "
                    "WHERE m.status IN ('NEEDS_REVIEW','AWAITING_CUSTOMER') ORDER BY m.id")
    if not cases:
        st.success("Queue is empty.")
        st.stop()
    st.dataframe(pd.DataFrame([{"ref": c["ref"], "status": status_badge(c["status"]), "customer": c["customer"] or "(unknown)",
                                "received": c["received_at"], "channel": c["channel"]} for c in cases]), hide_index=True)
    ref = st.selectbox("Open a case", [c["ref"] for c in cases])
    case = next(c for c in cases if c["ref"] == ref)
    mid = case["id"]
    left, right = st.columns([1, 1])
    with left:
        st.markdown(f"**{status_badge(case['status'])}**  ·  from `{case['sender']}`  ·  extractor: `{case['extractor']}`")
        with st.expander("Original message", expanded=True):
            st.code(case["raw_text"][:2500])
        ex = json.loads(case["extracted_json"])
        st.markdown("**What the system extracted**")
        st.dataframe(pd.DataFrame(ex["lines"])[["line_no", "part", "qty", "price", "description"]], hide_index=True)
        st.caption(f"PO: {ex['po_number']} · need-by: {ex['requested_date']} · ship-to text: {ex['ship_to_text']}")
    with right:
        excs = D.q(db, "SELECT * FROM exceptions WHERE message_id=? AND status='open'", (mid,))
        overrides = []
        st.markdown("**Open exceptions**")
        for e in excs:
            icon = "⛔" if e["severity"] == "block" else "ℹ️"
            st.markdown(f"{icon} **{e['code']}** · _{e['path']}_ — {e['message']}")
            fix = json.loads(e["fix_json"] or "{}")
            if e["severity"] != "block" or not fix:
                continue
            key, lab = f"fix_{e['id']}", fix.get("label", "Value")
            if fix["type"] == "select":
                v = st.selectbox(lab, [""] + fix["options"], key=key)
            elif fix["type"] == "number":
                v = st.number_input(lab, min_value=0, value=int(fix.get("default") or 0), key=key)
            elif fix["type"] == "bool":
                v = "approved:rep" if st.checkbox(lab, key=key) else ""
            else:
                v = st.text_input(lab, key=key)
            if v not in ("", 0, None):
                overrides.append({"code": e["code"], "line_no": e["line_no"], "value": str(v)})
        drafts = D.q(db, "SELECT * FROM outbound_emails WHERE message_id=? AND kind='clarification' ORDER BY id DESC LIMIT 1", (mid,))
        if drafts:
            d = drafts[0]
            st.markdown(f"**Drafted clarification email** ({d['status']})")
            st.text_area("To: " + d["to_addr"], d["body"], height=220, disabled=True, key=f"d{d['id']}")
            if d["status"] == "draft" and st.button("Send clarification to customer"):
                pipeline.send_draft(db, d["id"], actor="rep")
                st.rerun()
        if st.button("Apply fixes & re-validate", type="primary", disabled=not overrides):
            r = pipeline.apply_overrides(db, mid, overrides, actor="rep")
            st.success(f"Re-validated → {r['status']}")
            st.rerun()
    with st.expander("Audit trail"):
        st.dataframe(pd.DataFrame(D.q(db, "SELECT at, actor, action, detail FROM audit_log WHERE ref=? ORDER BY id", (ref,))), hide_index=True)

# ---------------------------------------------------------------- Customer portal: order form
elif page == "Place an order (portal)":
    st.title("Customer portal · place an order")
    st.caption("Structured entry removes extraction entirely - but the same validation rules still run.")
    contacts = D.q(db, "SELECT t.email, c.name, c.id cid FROM contacts t JOIN customers c ON c.id=t.customer_id ORDER BY c.name")
    who = st.selectbox("Signed in as", [f"{c['email']}  ({c['name']})" for c in contacts])
    me = contacts[[f"{c['email']}  ({c['name']})" for c in contacts].index(who)]
    sites = D.q(db, "SELECT code, address, city FROM ship_to WHERE customer_id=?", (me["cid"],))
    items = D.q(db, "SELECT sku, description, moq, pack_multiple FROM items ORDER BY sku")
    labels = {i["sku"]: f"{i['sku']} - {i['description']} (min {i['moq']}, packs of {i['pack_multiple']})" for i in items}
    with st.form("order"):
        po = st.text_input("PO number")
        need = st.date_input("Need by", value=date(2026, 11, 15))
        ship = st.selectbox("Ship to", [""] + [f"{s['code']} - {s['address']}, {s['city']}" for s in sites])
        rows = []
        for i in range(4):
            c1, c2 = st.columns([4, 1])
            sku = c1.selectbox(f"Item {i + 1}", [""] + list(labels), format_func=lambda s: labels.get(s, "-"), key=f"s{i}")
            qty = c2.number_input("Qty", min_value=0, value=0, key=f"q{i}")
            if sku and qty:
                rows.append([sku, int(qty)])
        go = st.form_submit_button("Submit order")
    if go:
        mid = pipeline.ingest_form(db, me["email"], po, need.isoformat(), ship.split(" - ")[0] if ship else None, rows)
        case = D.one(db, "SELECT * FROM messages WHERE id=?", (mid,))
        st.success(f"Reference **{case['ref']}** · {status_badge(case['status'])}")
        for e in D.q(db, "SELECT * FROM exceptions WHERE message_id=? AND status='open'", (mid,)):
            st.warning(e["question"] or e["message"])

# ---------------------------------------------------------------- Customer portal: status
elif page == "Order status (portal)":
    st.title("Customer portal · order status")
    ref, email = st.text_input("Reference (e.g. OTC-000019)"), st.text_input("Your email")
    if st.button("Look up") and ref and email:
        s = pipeline.case_status(db, ref, email)
        if not s:
            st.error("No matching order for that reference and email.")
        else:
            st.subheader(f"{s['ref']} · {status_badge(s['status'])}")
            if s["order"]:
                st.write(f"Sales order **{s['order']['bc_order_no']}** · PO {s['order']['po_number']} · total ${s['order']['total']:,.2f} · requested {s['order']['requested_date']}")
                st.dataframe(pd.DataFrame(s["lines"])[["line_no", "sku", "qty", "unit_price", "backorder_qty"]], hide_index=True)
            st.dataframe(pd.DataFrame(s["history"]), hide_index=True)

# ---------------------------------------------------------------- Systems
else:
    st.title("Connected systems (simulated)")
    t1, t2, t3, t4 = st.tabs(["Business Central sales orders", "HubSpot deals", "Customer email outbox", "Audit log"])
    t1.dataframe(pd.DataFrame(D.q(db, "SELECT number, customer_number, external_doc_no, status, tracking, created_at FROM mock_bc_sales_orders ORDER BY number")), hide_index=True)
    t2.dataframe(pd.DataFrame(D.q(db, "SELECT * FROM mock_hs_deals ORDER BY id")), hide_index=True)
    t3.dataframe(pd.DataFrame(D.q(db, "SELECT id, kind, status, to_addr, subject FROM outbound_emails ORDER BY id DESC LIMIT 200")), hide_index=True)
    t4.dataframe(pd.DataFrame(D.q(db, "SELECT at, actor, action, ref, detail FROM audit_log ORDER BY id DESC LIMIT 300")), hide_index=True)
