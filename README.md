# Hotel Operations Copilot

Mobile-first Streamlit demo with editable operating parameters, a separate AI chat workspace, and a FastAPI adapter for optional integrations. The web app and API share the same Python decision engines. Real credentials are not included in the project template.

## Architecture

```text
Streamlit web app ──┐
                    ├── shared Python operations service ── Excel demo workbook
WhatsApp webhook ─ FastAPI                         └────── SQLite session state
                         └── optional OpenAI Responses API
                         └── optional Meta WhatsApp Cloud API
```

The LLM classifies natural-language requests and optionally rewrites a response based on calculated facts. Python calculates the results. The WhatsApp adapter applies scenarios after confirmation; the web chat applies explicit parameter edits immediately and previews hypothetical requests. If `OPENAI_API_KEY` is blank, the deterministic intent parser is used. The WhatsApp webhook verifies Meta's HMAC signature and only processes senders listed in `WHATSAPP_ALLOWED_NUMBERS`.

For the single-manager demo, the Streamlit app and WhatsApp can share the same SQLite scenario/approval state. Set `WEB_MANAGER_ID=demo-gm`, then map the test number with `WHATSAPP_MANAGER_MAP=12125550123=demo-gm`. For multiple managers, map each number to a distinct manager ID. The unauthenticated local Streamlit demo is not an identity system; add real web sign-in before internet deployment.

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

### 5. Configure Meta WhatsApp Cloud API

1. Create or select a Meta business portfolio and a WhatsApp Business Account. Start with Meta's test number while building.
2. Create a Meta developer app and add the WhatsApp product. Record the app secret, access token, phone number ID, and the current Graph API version in `.env`.
3. Add a random string to `WHATSAPP_VERIFY_TOKEN`. This is the webhook verification token you choose; it is separate from the Meta access token and app secret.
4. Make the FastAPI service reachable at a **public HTTPS** URL. For local experiments use a reputable HTTPS tunnel; for a pilot deploy it behind a managed HTTPS endpoint. Meta cannot call `127.0.0.1`.
5. In the Meta app webhook settings, enter `https://YOUR_HOST/webhooks/whatsapp` and the exact `WHATSAPP_VERIFY_TOKEN`. Subscribe to the `messages` field for the WhatsApp Business Account.
6. Set `WHATSAPP_ALLOWED_NUMBERS` to the test manager's number including country code (digits; comma-separated for multiple managers). This allowlist is required before the webhook will respond.
7. Set `WHATSAPP_MANAGER_MAP` to map that same number to the shared web manager ID, for example `12125550123=demo-gm`.
8. Restart `uvicorn`, send a WhatsApp message to the test number, then test: “Are we okay today?”, “What if one person calls out?”, “Run scenario”, and “Cancel”.

The webhook currently replies to inbound text and interactive quick-reply messages. It does not send proactive alerts or templates. Business-initiated messages outside WhatsApp's customer-support window require an approved template; add those only when proactive notifications are in scope.

### 6. Connect a web frontend to the API (optional)

The current Streamlit web app remains usable as-is and shares the same operations and LLM modules. Another web client can call:

- `GET /api/today` for calculated hotel status
- `POST /api/chat` with `{"manager_id":"demo-manager","message":"Will rooms be ready?"}`

Send `X-API-Key: <COPILOT_API_KEY>` only from trusted server-side code. For an internet-facing web app, replace the prototype shared key with authenticated GM accounts and hotel/role authorization.

## WhatsApp manager commands

- Read-only questions: current status, room readiness, staffing explanation, inventory order.
- Scenario proposals: call-outs, budget change, or tequila arrival timing. The bot asks for `RUN SCENARIO` or `CANCEL` before applying to that manager's session.
- `RESET DEMO` clears that manager's session scenarios; it does not edit the workbook.
- `APPROVE DRAFT` records an approval event only. No order is sent and no schedule is distributed.

## Streamlit pages

- **Today** shows the current hotel summary.
- **Parameters** lets the manager adjust room cleaning and inspection times, shift capacity assumptions, staffing adjustment, inventory forecast inputs, safety stock, and budget. Results update immediately and apply across the other pages for the current app session. Parameter changes invalidate any prior approval draft and are written to the audit history.
- **AI Chat - My Copilot** offers suggested questions, concise OpenAI answers grounded in current calculations, and explicit session parameter edits. Changes show before/after results and can be undone. “What if” requests preview the impact without applying it. Ambiguous room types require clarification; values are validated before the plan changes. Conversations can be downloaded as Markdown. If OpenAI fails, the app clearly labels its local calculation summary.
- **Operations** retains housekeeping plans, inventory details, quick scenarios, and draft approval.

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

## Chat examples

- “Explain the staffing plan.”
- “Add 5 minutes to checkout cleaning.”
- “What if I increase checkout cleaning by 5 minutes?”
- “Set stayover cleaning to 12 minutes.”
- “Set the budget to $9,000.”

Session edits survive navigation within the current browser session. They do not modify the workbook and are lost when a new session starts. Undo restores the last chat edit only if no later parameter or scenario changes occurred. Readiness uses a simplified room assignment model; breaks and other duties reduce capacity but are not scheduled on its timeline.
