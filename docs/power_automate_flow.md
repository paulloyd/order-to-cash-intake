# Power Automate companion flow - DESIGN SPEC (not built or exported)

I did not build this inside a Power Automate tenant, so there is no `.zip` export. Build it in half a day and capture screenshots.

**Purpose:** approvals for the internal exception types (credit hold/limit, price variance) inside Teams/Outlook instead of the Streamlit queue.

1. **Trigger:** HTTP request (When an HTTP request is received) called by the API when a case has `CREDIT_*` or `PRICE_MISMATCH` exceptions. Body: ref, customer, order value, message.
2. **Action:** *Start and wait for an approval* (Approve/Reject, assigned to the finance or sales-manager group).
3. **Condition:** outcome = Approve.
   - Yes: call `POST /cases/{ref}/resolve` with `[{"code":"CREDIT_LIMIT","value":"approved:<approver>"}]`.
   - No: post to the customer-service channel with the ref and rejection comment; leave the case in NEEDS_REVIEW.
4. **Teams post:** adaptive card with ref, exceptions, order value, a link to the dashboard.
5. **Error handling:** configure run-after on failure to notify the owner; retry policy on the HTTP actions.

Talking point: the approval logic stays in one deterministic place (the API); Power Automate only handles routing and the human decision.
