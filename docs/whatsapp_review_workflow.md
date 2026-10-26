# Shared app and WhatsApp review workflow — local development slice

This revision composes **one injected `WorkflowStore` and private file store**
across signed WhatsApp intake, a separate worker, and an authenticated private
app. It does not change the public Streamlit deployment, enroll partners, create
credentials, or enable general live uploads. All integration tests use fictional
data, fake outbound transport and explicitly configured synthetic identities.

## Implemented paths

- A supported native-text PDF passes the existing exact enrolled-profile,
  seven-date/49-cell, arithmetic, saved-room-capacity and current-week checks.
  The optional durable receipt precedes calculation; the versioned PDF follows.
- A PNG/JPEG or one-page scanned/image-backed PDF is assessed **locally**.
  Image-backed PDFs, hidden text and Form XObjects cannot pretend to be trusted
  native text. A complete candidate that passes all structural/date/count checks
  receives a full numeric review message, never an automatically generated roster.
- The same enrolled sender replies `CONFIRM <code>` or `CANCEL <code>`. Confirmation
  is bound to immutable source bytes, candidate rows, tenant/business/sender,
  saved Parameters/staff revision and a 30-minute expiry. It also requires the
  original prompt to have been accepted by the provider. A settings change,
  wrong sender, stale date, altered source, expired code or cancellation cannot
  generate a roster. Corrections require cancelling and resending a clearer file;
  arbitrary conversational edits are not interpreted as new counts.
- Confirmation calculates through the **existing deterministic weekly scheduler**
  and directly queues one result PDF, without an extra working receipt. Same-message
  retries, repeated confirmation and concurrent confirmations cannot duplicate the
  result. Crashes resume the bound confirming job; terminal calculation failure
  produces an honest failure notice instead of a promised report.
- The private app immediately reads the saved version from that same store.
  Parameters/staff changes require a matching revision, and authentication—not
  client-supplied tenant IDs—determines hotel scope. Version/PDF reads are scoped
  to the authenticated hotel. The browser token stays in memory, not URLs/storage;
  logout/new login aborts and fences stale responses.

## Entrypoints and verification

`api.shared_forecast_workflow.create_shared_workflow_app(...)` composes the exact
injected adapters. `TwilioWebhookConfig` pins externally configured HTTPS URLs,
account and business sender. Webhooks verify all form fields and only enqueue;
OCR/calculation/sending are never background tasks in an HTTP request. Supervise
the worker separately. There is no public enrollment, worker-trigger or raw-file
endpoint, and no automatic environment/default-tenant initialization.

`api.private_hotel_app.create_private_app(...)` also works independently over the
same injected store. Default authentication denies access. `TokenMapAuthenticator`
is an explicit server-configured pilot adapter, not production SSO; it never
creates tokens/accounts. `/private` is a login shell; every `/private/api/...`
request needs valid bearer authentication. Do not use test tokens for deployment.

`services.forecast_media_review.evaluate_forecast_media(...)` returns extraction
evidence without saving, scheduling or sending. `forecast_media_worker` wraps it
in a Linux child with a 35-second total deadline, bounded input/output, network
syscall denial inherited by Tesseract, a minimal credential-free environment,
private disposable temporary files and a per-process address-space limit. In the
review-enabled workflow, **all initial PDF inspection/decompression** also runs
inside this child, not just the fallback OCR step. OCR
confidence and two same-engine passes are **not proof of digit accuracy**: OCR
always remains human-review-required, including a plausible departures misread
that happens to satisfy every arithmetic total.

The ordinary worker requires `WHATSAPP_REVIEW_ENABLED=1` to enable this local
review path. It remains off by default. General live review is explicitly blocked
in `ForecastWorker`; the earlier exact-fixture live pilot is unchanged. The
existing manual app's OCR remains review-required, with bounded preprocessing
that never substitutes characters or invents dates. Conflicting recovered cells
are not selected as if certain.

Focused verification:

```bash
python -m pytest -q tests/test_forecast_media_review.py tests/test_manual_ocr_recovery.py \
  tests/test_forecast_media_worker.py tests/test_whatsapp_review.py \
  tests/test_private_hotel_app.py tests/test_shared_forecast_workflow.py
```

## Calculation scope—not occupancy prediction

The report supplies occupancy/room forecasts; the app calculates cleaning demand
and staff assignments from them. Weekly cleaning minutes are departures × checkout
minutes plus **all** stayovers × stayover minutes. Productive shift time deducts
breaks and other duties. Availability, role minimums, paid-hour limits, locks and
manual assignments constrain allocation; unresolved shortages remain visible.

The saved inspection buffer is **not included in weekly cleaning demand**.
Stayover-policy text is descriptive and does not filter room counts. These limits
are shown in the private app and PDF; this revision does not invent an inspection
allocation rule. Demand headcount is not proof that eligible staff can be assigned.

## Before a real deployment

1. Approve the exact private application/hosting/authentication and data scope;
   public Streamlit must not be pointed at private hotel data without its own
   authenticated integration. Existing demo data is not real hotel enrollment.
2. Supply approved identity-provider/manager-to-hotel mapping and secure secret
   configuration. Establish roles, revocation, audit/retention/backups and a
   reviewed durable database/private-storage deployment. SQLite/files here remain
   explicitly single-host development adapters, not a production readiness claim.
3. Add the reviewed live review-runtime admission/budget adapter and private
   expiring provider-media serving route. The current composition deliberately
   has no public media endpoint and cannot bypass the live/development-store gate.
4. Build/profile the OCR-enabled runtime separately. The deployed narrow pilot
   image intentionally has no Tesseract/PyMuPDF. Per-process caps are not an
   aggregate 512MiB guarantee: permit only one OCR job at a time and measure the
   web+worker+Tesseract peak on the target host before choosing a service tier.
5. Approve a new, bounded live test and sending/usage limits. Preserve every
   previous attempt/audit; do not reset old budgets or silently widen expiry.
   Current/historical evaluation clocks never silently redate a real report.

Local success is not publication, deployment, production authentication or proof
that every camera photo will extract correctly. Bad/unclear files must be reviewed
or resent, not guessed into a staffing schedule.
