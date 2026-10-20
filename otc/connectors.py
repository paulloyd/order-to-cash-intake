"""Integration layer. Each system has a mock (offline, backed by SQLite) and a live HTTP client.

HONESTY NOTE: the mocks are what the demo and tests exercise. The Live* classes follow the vendors' documented
REST shapes but were NOT run against a real Business Central / HubSpot / Magento tenant by the author.
Set OTC_BC_MODE / OTC_HUBSPOT_MODE / OTC_MAGENTO_MODE=live plus the env vars in .env.example to use them.
"""
import json, os, itertools
from . import config
from .db import q, one, now_iso


class IntegrationError(Exception):
    pass


# ============================================================ Business Central
def bc_payload(customer_no, po, requested_date, lines):
    """Shape of a Business Central API v2.0 salesOrders document (+ nested salesOrderLines)."""
    return {
        "customerNumber": customer_no,
        "externalDocumentNumber": po or "",
        "requestedDeliveryDate": requested_date,
        "salesOrderLines": [{"lineType": "Item", "lineObjectNumber": l["sku"], "quantity": l["qty"],
                             "unitPrice": l["unit_price"]} for l in lines],
    }


class MockBC:
    """Mimics the guards a real ERP enforces, so the pipeline's error handling is exercised."""
    def __init__(self, db):
        self.db = db

    def create_sales_order(self, payload, at=None):
        if not one(self.db, "SELECT 1 x FROM customers WHERE account_no=?", (payload["customerNumber"],)):
            raise IntegrationError(f"BC: customer {payload['customerNumber']} not found")
        for l in payload["salesOrderLines"]:
            if not one(self.db, "SELECT 1 x FROM items WHERE sku=?", (l["lineObjectNumber"],)):
                raise IntegrationError(f"BC: item {l['lineObjectNumber']} not found")
        if payload["externalDocumentNumber"] and one(
                self.db, "SELECT 1 x FROM mock_bc_sales_orders WHERE customer_number=? AND external_doc_no=?",
                (payload["customerNumber"], payload["externalDocumentNumber"])):
            raise IntegrationError("BC: externalDocumentNumber already used for this customer")
        n = (one(self.db, "SELECT COUNT(*) c FROM mock_bc_sales_orders")["c"]) + 101001
        number = f"SO-{n}"
        self.db.execute("INSERT INTO mock_bc_sales_orders VALUES (?,?,?,?,?,?,?)",
                        (number, payload["customerNumber"], payload["externalDocumentNumber"], json.dumps(payload),
                         "Open", None, at or now_iso()))
        return {"number": number, "status": "Open"}

    def shipped_orders(self):
        return q(self.db, "SELECT number, tracking FROM mock_bc_sales_orders WHERE status='Shipped'")

    def ship(self, number, tracking):                     # simulates the warehouse posting a shipment
        self.db.execute("UPDATE mock_bc_sales_orders SET status='Shipped', tracking=? WHERE number=?", (tracking, number))


class LiveBC:
    """Business Central cloud API v2.0 with OAuth2 client-credentials. Untested against a live tenant."""
    def __init__(self):
        import httpx
        self.httpx = httpx
        self.tenant, self.env, self.company = (os.environ[k] for k in ("BC_TENANT_ID", "BC_ENVIRONMENT", "BC_COMPANY_ID"))
        self.cid, self.secret = os.environ["BC_CLIENT_ID"], os.environ["BC_CLIENT_SECRET"]
        self.base = f"https://api.businesscentral.dynamics.com/v2.0/{self.tenant}/{self.env}/api/v2.0/companies({self.company})"

    def _token(self):
        r = self.httpx.post(f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token", data={
            "grant_type": "client_credentials", "client_id": self.cid, "client_secret": self.secret,
            "scope": "https://api.businesscentral.dynamics.com/.default"}, timeout=30)
        r.raise_for_status()
        return r.json()["access_token"]

    def create_sales_order(self, payload, at=None):
        h = {"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"}
        head = {k: v for k, v in payload.items() if k != "salesOrderLines"}
        r = self.httpx.post(f"{self.base}/salesOrders", json=head, headers=h, timeout=30)
        if r.status_code >= 300:
            raise IntegrationError(f"BC {r.status_code}: {r.text[:200]}")
        so = r.json()
        for l in payload["salesOrderLines"]:
            lr = self.httpx.post(f"{self.base}/salesOrders({so['id']})/salesOrderLines", json=l, headers=h, timeout=30)
            if lr.status_code >= 300:
                raise IntegrationError(f"BC line {lr.status_code}: {lr.text[:200]}")
        return {"number": so["number"], "status": so.get("status", "Open")}

    def shipped_orders(self):
        h = {"Authorization": f"Bearer {self._token()}"}
        r = self.httpx.get(f"{self.base}/salesShipments", headers=h, timeout=30)
        r.raise_for_status()
        return [{"number": s.get("orderNumber"), "tracking": s.get("number")} for s in r.json().get("value", [])]


# ============================================================ HubSpot
class MockHubSpot:
    def __init__(self, db):
        self.db = db

    def upsert_contact(self, email, name, company):
        r = one(self.db, "SELECT hs_id FROM mock_hs_contacts WHERE email=?", (email,))
        if r:
            return r["hs_id"]
        hs_id = f"hs-{one(self.db, 'SELECT COUNT(*) c FROM mock_hs_contacts')['c'] + 1001}"
        self.db.execute("INSERT INTO mock_hs_contacts VALUES (?,?,?,?)", (email, name, company, hs_id))
        return hs_id

    def create_deal(self, name, amount, stage, contact_email, case_ref):
        did = f"deal-{one(self.db, 'SELECT COUNT(*) c FROM mock_hs_deals')['c'] + 5001}"
        self.db.execute("INSERT INTO mock_hs_deals VALUES (?,?,?,?,?,?)", (did, name, amount, stage, contact_email, case_ref))
        return did

    def update_deal_stage(self, deal_id, stage):
        self.db.execute("UPDATE mock_hs_deals SET stage=? WHERE id=?", (stage, deal_id))


class LiveHubSpot:
    """HubSpot CRM v3/v4 via a private-app token. Untested against a live portal."""
    def __init__(self):
        import httpx
        self.httpx = httpx
        self.h = {"Authorization": f"Bearer {os.environ['HUBSPOT_TOKEN']}", "Content-Type": "application/json"}
        self.base = "https://api.hubapi.com"

    def upsert_contact(self, email, name, company):
        s = self.httpx.post(f"{self.base}/crm/v3/objects/contacts/search", headers=self.h, timeout=30, json={
            "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}]})
        s.raise_for_status()
        if s.json().get("results"):
            return s.json()["results"][0]["id"]
        first, _, last = (name or "").partition(" ")
        r = self.httpx.post(f"{self.base}/crm/v3/objects/contacts", headers=self.h, timeout=30, json={
            "properties": {"email": email, "firstname": first, "lastname": last, "company": company}})
        r.raise_for_status()
        return r.json()["id"]

    def create_deal(self, name, amount, stage, contact_email, case_ref):
        contact_id = self.upsert_contact(contact_email, "", "")
        r = self.httpx.post(f"{self.base}/crm/v3/objects/deals", headers=self.h, timeout=30, json={
            "properties": {"dealname": name, "amount": str(amount), "dealstage": stage, "pipeline": "default"}})
        r.raise_for_status()
        did = r.json()["id"]
        self.httpx.put(f"{self.base}/crm/v4/objects/deals/{did}/associations/default/contacts/{contact_id}",
                       headers=self.h, timeout=30)
        return did

    def update_deal_stage(self, deal_id, stage):
        self.httpx.patch(f"{self.base}/crm/v3/objects/deals/{deal_id}", headers=self.h, timeout=30,
                         json={"properties": {"dealstage": stage}}).raise_for_status()


# ============================================================ Magento
class MockMagento:
    """Serves orders shaped like Magento's GET /rest/V1/orders items (subset). Source: data/magento_orders.json."""
    def __init__(self, db):
        self.db = db

    def fetch_new_orders(self):
        path = config.DATA_DIR / "magento_orders.json"
        if not path.exists():
            return []
        done = {r["increment_id"] for r in q(self.db, "SELECT increment_id FROM mock_magento_imported")}
        return [o for o in json.loads(path.read_text()) if o["increment_id"] not in done]


class LiveMagento:
    """Magento 2 REST: GET /rest/V1/orders?searchCriteria[...]. Untested against a live store."""
    def __init__(self):
        import httpx
        self.httpx = httpx
        self.base = os.environ["MAGENTO_BASE_URL"].rstrip("/")
        self.h = {"Authorization": f"Bearer {os.environ['MAGENTO_TOKEN']}"}

    def fetch_new_orders(self):
        r = self.httpx.get(f"{self.base}/rest/V1/orders", headers=self.h, timeout=30, params={
            "searchCriteria[filterGroups][0][filters][0][field]": "status",
            "searchCriteria[filterGroups][0][filters][0][value]": "pending"})
        r.raise_for_status()
        return r.json().get("items", [])


def get_bc(db):
    return LiveBC() if config.BC_MODE == "live" else MockBC(db)


def get_hubspot(db):
    return LiveHubSpot() if config.HUBSPOT_MODE == "live" else MockHubSpot(db)


def get_magento(db):
    return LiveMagento() if config.MAGENTO_MODE == "live" else MockMagento(db)
