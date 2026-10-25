# Hotel Operations Copilot

Mobile-first Streamlit demo with editable operating parameters, a separate AI chat workspace, and a FastAPI adapter for optional integrations. The web app and API share the same Python decision engines. Real credentials are not included in the project template.

## WhatsApp-first forecast implementation (development only)

A new signed webhook → durable job → strict forecast validation → versioned roster
→ PDF outbox workflow is implemented with **fake transport by default**. Run the
[synthetic end-to-end demo and review production gates](docs/whatsapp_forecast_workflow.md).
It is not connected to a live number. Native PDFs require an explicitly enrolled
profile; the supported existing daily-table layout accepts its known labels and
unambiguous full M/D/YYYY dates. Photos/scans still request correction. Parameters
now has a shared saved **Total rooms** field (292 for this built-in demo, per the
user's latest instruction), used with the same saved cleaning standards by the
WhatsApp worker; no separate enrollment capacity is maintained. The
legacy WhatsApp text scenario webhook described below has been replaced; the
manual app and web chat remain available. Do not follow the older live-enable
steps below for this workflow; credentials alone cannot activate it.

## Architecture

```text
Streamlit manual app ── shared Python operations / roster service
WhatsApp signed webhook ── durable jobs ── separate worker ── versioned roster + PDF outbox
                                                └── fake provider (default)
                                                └── gated Meta transport (not live)
```

The manual web chat optionally uses an LLM to classify requests and compose
responses; Python calculates the results. WhatsApp forecast processing does not
use an LLM. Exact operator enrollment binds each sender and business number to
a hotel/settings record. Legacy phone allowlist/manager-map environment variables
do not enroll forecast senders. Local development can share the configured
`COPILOT_STATE_DB` and enrolled `WEB_MANAGER_ID`; the unauthenticated public app
blocks access to enrolled manager records and is not a production identity system.

## Publish a public demo on Streamlit Community Cloud

1. Create a GitHub repository containing this project; keep the source repository private if you prefer.
2. In Community Cloud, create an app from that repository with `app.py` as the entrypoint.
3. If the repository is private, change the Streamlit app sharing setting to public so the demo URL works for anyone.
4. To enable OpenAI in the hosted chat, add `OPENAI_API_KEY` and optionally `OPENAI_MODEL` under app Settings → Secrets. The app also works with local calculated answers when the key is omitted. Never commit `.env` or `secrets.toml`.

The demo workbook is synthetic. Each cloud visitor receives a separate session state. Local runs may continue to use `WEB_MANAGER_ID` from `.env`. The public demo is not a production system and does not include authentication or real hotel integrations.

## Step-by-step setup

### 1. Install and configure locally

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set unique values for `COPILOT_STATE_KEY`, `COPILOT_API_KEY`, and `WHATSAPP_VERIFY_TOKEN`. Keep real secrets in `.env`; never commit that file. Leave `OPENAI_API_KEY` blank if you want to run without the LLM.

### 2. Run the web app

```sh
streamlit run app.py
```

The app reads `HOTEL_OPS_WORKBOOK`, defaulting to `data/Hotel_Ops_Demo_Data.xlsx`. The AI Chat - My Copilot screen uses the optional LLM when configured and falls back to deterministic intent matching otherwise.

### 3. Run the API

In a second terminal, with the virtual environment active:

```sh
uvicorn api.main:app --host 127.0.0.1 --port 8000 --reload
```

Check `http://127.0.0.1:8000/health` and `http://127.0.0.1:8000/docs`. The `/api/today` and `/api/chat` endpoints require the `X-API-Key` header set to the `COPILOT_API_KEY` value. Do not put this shared server key in browser JavaScript or a public mobile app. A production web frontend should authenticate users on the server and call the API server-side, or use proper user authentication instead of this prototype key.

### 4. Enable OpenAI responses (optional)

Create an API key in the OpenAI platform and set it as `OPENAI_API_KEY` in `.env`. Keep `OPENAI_MODEL` configurable so the model can be changed without code edits. The LLM can interpret phrasing and make factual responses more natural; it does not calculate staffing or purchasing values. `store: false` is set for Responses API requests. The fallback keeps the demo functional if the key is absent or the request fails.

### 5. Test the WhatsApp forecast workflow (synthetic only)

Follow the [development workflow guide](docs/whatsapp_forecast_workflow.md) for
the runnable signed-webhook demo, strict accepted PDF profile, enrollment and
separate worker. The default configuration does not contact Meta. Live mode is
blocked with the development storage adapters even if credentials are present.
A separate pilot business number, authorized Meta setup, durable hosting/storage,
authenticated app access and an explicitly approved real-message test remain
production gates. No actual number or account has been connected by this change.

### 6. Connect a web frontend to the API (optional)

The current Streamlit web app remains usable as-is and shares the same operations and LLM modules. Another web client can call:

- `GET /api/today` for calculated hotel status
- `POST /api/chat` with `{"manager_id":"demo-manager","message":"Will rooms be ready?"}`

Send `X-API-Key: <COPILOT_API_KEY>` only from trusted server-side code. For an internet-facing web app, replace the prototype shared key with authenticated GM accounts and hotel/role authorization.

## WhatsApp forecast messages

- Send the complete supported native-text forecast PDF to the enrolled business number.
- Valid documents create a saved, versioned roster and same-sender PDF reply.
- Unclear/invalid documents request correction; photos and scans require review.
- Shortages are labeled **NEEDS ATTENTION**, not silently resolved.
- Text/unsupported messages receive upload guidance; legacy scenario commands are no longer handled by the webhook.
- The implementation currently runs with a fake transport only. See the guide before any live setup.

## Streamlit pages

- **Today** and **Operations** are temporarily hidden to keep the demo focused. Their renderers and services remain intact; remove their names from `HIDDEN_NAVIGATION` in `app.py` to restore them. Sessions left on a hidden page return to Weekly Forecast.
- **Parameters** lets the manager adjust room cleaning and inspection times, shift capacity assumptions, staffing adjustment, inventory forecast inputs, safety stock, and budget. Daily results update immediately. Checkout and stayover cleaning times are shared with saved Staff & Settings and the weekly planner (demo defaults: 22 and 10 minutes). Weekly forecast room counts remain unchanged; select **Regenerate weekly roster** to recalculate the weekly draft. Other daily/inventory inputs remain separate from the weekly staffing assumptions. Parameter changes invalidate any prior approval draft and are written to the audit history.
- **AI Chat - My Copilot** offers suggested questions, concise OpenAI answers grounded in current calculations, and explicit session parameter edits. Changes show before/after results and can be undone. “What if” requests preview the impact without applying it. Ambiguous room types require clarification; values are validated before the plan changes. Conversations can be downloaded as Markdown. If OpenAI fails, the app clearly labels its local calculation summary.
- **Operations** retains housekeeping plans, inventory details, quick scenarios, and draft approval behind the temporary navigation hide list.
- **Staff & Settings** maintains 24 fictional employees (16 housekeepers, 4 housemen, 2 lobby staff, 2 managers) and editable demo assumptions for availability, time off, shift patterns, hour limits, cleaning standards, stayover service, role coverage, breaks, and status codes. Edits are validated and saved in the app's SQLite state, separate from the workbook. Each save defaults the dataset to **OPERATIONAL**; users must explicitly confirm that the entire roster/settings remain fictional to keep demo reset enabled. Saved staff and assumptions drive the weekly planner. Checkout/stayover cleaning standards also synchronize with Parameters and explicit chat edits; daily staffing still uses the separate workbook roster. Save or cancel form edits explicitly.
- **Weekly Forecast** is the first/default page. Uploading a clear, validated native-text forecast automatically produces a labeled, unsaved draft roster. OCR results and extraction ambiguity must be reviewed before any draft can be generated. “View extracted forecast data” opens an editable dates-as-rows, metrics-as-columns review; the forecast must be confirmed before a roster can be saved. The validator checks all seven Friday–Thursday days and the totals `arrivals + stayovers = rooms on books` and `rooms on books + forecast pickup = forecast occupied`. Prior-day occupancy remains unknown unless supplied. Arrivals are not counted as additional checkout cleans. The deterministic weekly employee-shift roster uses saved availability, requested time off, weekly/daily limits, cleaning standards, paid/productive time settings, and minimum role coverage. Uncovered demand, unavailable shifts, unresolved times, and conflicting locked edits are shown; the app does not assign individual rooms. Manual edits can be locked across regeneration, including unsaved preview edits. A changed forecast, staff roster, or saved assumption marks the previous preview **OUT OF DATE**; it cannot be saved or exported until **Regenerate weekly roster** is selected. The collapsed **Day-by-day calculation summary** reveals all seven days together: checkout/stayover counts × cleaning minutes, total workload, rounded-up standard-shift staffing need, actual scheduled cleaning coverage, and uncovered demand/constraints. The summary uses the exact inputs captured for that generated roster; increased workload does not always cross a headcount threshold. Changes are shown before saving a new roster version. Excel and PDF exports include the Fri–Thu grid and validation notes.

Forecast vision comparison is optional, default-off, and limited to an explicitly allowlisted localhost request. It makes one OpenAI Responses API request only after a per-file opt-in, uses the server-side `OPENAI_API_KEY`, and may incur API charges. It compares AI cell candidates and visible evidence against local extraction; it never auto-overwrites populated local values. Conflicts, uncertain/missing cells, and arithmetic/date validation must be resolved by a human before roster generation. The public Streamlit demo has no authentication and cannot invoke the paid vision feature. AI extraction has not been benchmarked against a representative labeled forecast set; no accuracy advantage is claimed.

## Pilot hardening before real operations

- Move from the local SQLite file to a managed database before multiple app instances or hotels use the service.
- Add role-based GM identity and hotel scoping; phone allowlisting is only a prototype gate.
- Add webhook retry handling, outbound message-status tracking, alerting, backup/retention policies, and rate limits.
- Add approved WhatsApp templates before proactive messages.
- Test the scheduler and all decision engines against the supplied fixture before presenting outputs as operational recommendations. Keep approval as draft-only until downstream systems are deliberately integrated.

## Tests

```sh
pytest -q
```

Local image and scanned-PDF OCR uses Tesseract without sending the file to an external OCR service. Install Tesseract locally (for example, `brew install tesseract`) before uploading images/scans. Streamlit Community Cloud installs the system package listed in `packages.txt`; the Python readers/OCR/export dependencies are in `requirements.txt`. Native text PDFs work without OCR. The review table and manual-entry path are available when OCR is missing or uncertain. PDFs are limited to 10 pages and images to 40 megapixels in this demo.

For optional private localhost vision testing, use a server-side key already configured in `.env`, set `FORECAST_AI_PRIVATE_ENABLED=1`, and set `FORECAST_AI_ALLOWED_MANAGER_ID` to the local `WEB_MANAGER_ID`. Calls remain disabled from non-loopback clients even when these settings are present. There is no browser-side key, and the public hosted app cannot use this feature; do not enable it for unauthenticated public visitors.

## Chat examples

- “Explain the staffing plan.”
- “Add 5 minutes to checkout cleaning.”
- “What if I increase checkout cleaning by 5 minutes?”
- “Set stayover cleaning to 12 minutes.”
- “Set the budget to $9,000.”

Session parameter and chat edits survive navigation within the current browser session. Staff & Settings and saved weekly roster versions persist in SQLite and do not modify the workbook. Undo restores the last chat edit only if no later parameter or scenario changes occurred. Readiness uses a simplified room assignment model; breaks and other duties reduce capacity but are not scheduled on its timeline. The weekly generator reports whether the current aggregate demand and editable staffing constraints appear covered; it does not build room-level assignments, publish/distribute schedules, or replace manager review. Run locally with `streamlit run app.py`. SQLite persistence is suitable for a single local instance, but a hosted Streamlit filesystem may be ephemeral across restarts and does not coordinate multiple replicas; use durable managed storage for hosted operational data. The public demo has no sign-in, so do not enter real employee or hotel operating data.
