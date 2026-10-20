# Setup & capture checklist

## 1. Run it offline (5 minutes, no accounts needed)
1. `python -m venv .venv && source .venv/bin/activate`
2. `pip install -r requirements.txt`
3. `python run_demo.py` - seeds data, generates ~100 messy orders, processes them, prints results.
   **Screenshot:** the terminal summary.
4. `streamlit run app/streamlit_app.py`
   **Screenshots:** Dashboard; Exception queue with a case open (message, extraction, drafted email); Place an order; Order status timeline; Systems & outbox.
5. `python -m pytest tests -q` - 16 tests.

## 2. Turn on the AI extractor (optional)
1. `cp .env.example .env`, add `ANTHROPIC_API_KEY`, then `export $(grep -v '^#' .env | xargs)`.
2. `OTC_EXTRACTOR=llm python run_demo.py`. Failures fall back to the rules parser and are labelled `rules(fallback)`.
3. Compare the results block with the rules-only run and **report what actually changed** - the LLM path is not benchmarked in this repo.

## 3. Drive it from n8n
1. `uvicorn otc.api:app --port 8000`
2. Run n8n (`npx n8n`), import `workflow/n8n_order_intake.json`, set the IMAP and SMTP credentials, point URLs at your API host.
3. Send yourself a test order email. **Screenshot:** the execution graph and the dashboard row.
4. The JSON follows n8n's node structure but has not been import-tested by the author; expect to adjust node parameters.

## 4. Go live against real systems (optional, untested path)
- **Business Central:** free sandbox via a Microsoft 365 developer/trial tenant; register an Entra app, fill the `BC_*` variables, set `OTC_BC_MODE=live`. See `docs/business_central_field_mapping.md`.
- **HubSpot:** create a private app with CRM contacts/deals scopes, set `HUBSPOT_TOKEN`, `OTC_HUBSPOT_MODE=live`.
- **Magento:** Open Source in Docker or any store with an integration token; set `MAGENTO_BASE_URL`, `MAGENTO_TOKEN`, `OTC_MAGENTO_MODE=live`.
Record in your notes exactly which of these you ran; only claim what you ran.
