# WhatsApp-first forecast pilot — implemented development boundary

**Not deployed or connected to WhatsApp. No actual number, credentials, account,
external message, or production hotel data is included.** A separate pilot
business number is the planned production identity. Provisioning it remains a
separate approval/configuration task; the code does not buy or activate numbers.

## What works

1. Meta-shaped signed webhook verifies HMAC over a bounded raw body (1 MiB).
2. Exact `(business phone number ID, sender)` enrollment maps to one explicit
   tenant/hotel/settings record. Unknown senders/IDs get no fetch or reply.
3. Webhook commits a job and returns; no PDF parsing, scheduling or external
   request runs inside the webhook or Streamlit process.
4. Independent worker fetches bounded media, verifies its SHA-256 and validates
   the explicitly enrolled report profile, dates, required cells, arithmetic,
   current saved Parameters room inventory and occupancy percentage. Supplied
   hotel identifiers must agree with enrollment; headerless profiles rely on
   explicit single-hotel sender enrollment, not invented document identity.
5. Latest persisted app Parameters/staff/settings/availability and existing locked/manual assignments feed
   the existing deterministic scheduler. No staff, hotel or missing cell is guessed.
6. Atomic database transaction appends an immutable roster version, updates the
   latest app snapshot and creates a same-sender PDF outbox entry. Original
   versions remain accessible. Changes are serialized with manual app saves.
7. PDF includes the weekly staff grid, locked markers and seven daily calculations.
   Shortages/conflicts are **NEEDS ATTENTION**, not automatically “solved.”
8. Separate dispatch enforces enrollment and the last-inbound 24-hour window.
   The default **fake** adapter records a simulated acceptance; it sends nothing.
9. The existing manual app workflow remains available. A local synthetic inbox
   lists saved WhatsApp versions without replacing an open draft. WhatsApp-linked
   manager data is blocked app-wide on non-loopback or unknown clients. This is
   a development containment measure, **not** production authentication.

The previous unqueued WhatsApp text/scenario command path is replaced by this
forecast-intake path; unsupported/text inputs receive upload instructions.
The manual web app and `/api/chat` path remain unchanged.

## Run the complete synthetic demonstration

Use the project virtual environment with `requirements.txt` installed:

```sh
python -m scripts.demo_whatsapp_workflow --output /tmp/whatsapp-pilot-demo
```

The output directory must not already exist. It produces:

- `synthetic-forecast.pdf`: entirely fictional accepted source template.
- `synthetic-weekly-roster.pdf`: the generated shortage example with saved lock.
- `demo-summary.json`: proof of signed intake, prior version preservation,
  lock preservation, fake same-sender reply and duplicate suppression.
- `runtime/workflow.sqlite3` and `runtime/private/`: development persistence.

The script uses an in-process HTTP TestClient, a fixed synthetic clock, temporary
synthetic signature secret and a fake media transport. It does **not** contact
Meta, use real credentials, or send messages. Acceptance is not delivery.

## Explicit, narrowly validated document profiles

`canonical_v1` remains the default; there is no automatic profile detection. It
requires a native-text PDF with an exact enrolled Hotel code and ISO dates:

```text
Hotel code: SYNTHETIC-HOTEL
Dates: 2026-10-23 2026-10-24 2026-10-25 2026-10-26 2026-10-27 2026-10-28 2026-10-29
Departures: 30 30 30 30 30 30 30
Stayovers: 60 60 60 60 60 60 60
Arrivals: 40 40 40 40 40 40 40
Rooms on books: 100 100 100 100 100 100 100
Forecast pickup: 10 10 10 10 10 10 10
Forecast occupied: 110 110 110 110 110 110 110
Occupancy %: 27.5 27.5 27.5 27.5 27.5 27.5 27.5
```

This example is for an explicitly enrolled 400-room fictional hotel. ISO dates,
full Friday–Thursday week, exact unique enrolled hotel code, seven values per
canonical row, complete counts, consistent totals and capacity are mandatory. Departing and staying rooms must
fit the inventory as disjoint room sets in this strict standard report profile;
prior-day occupancy is still left unknown rather than populated by inference.
The accepted week starts between seven days before and 56 days after the worker
clock. Occupancy percentage permits 0.6 percentage points of source rounding.
No extra numeric footer is accepted. PDFs: 10 MiB/10 pages; images: 5 MiB/40 MP.
Encrypted/active/malformed PDFs fail closed. Files are data, not instructions.

### Existing hotel daily-table format

An operator can explicitly enroll `report_profile="hotel_daily_table_v1"` for a
sender mapped to exactly one hotel. This profile matches the observed report's
whole-document structure: Friday-through-Thursday weekday header, seven full
unambiguous M/D/YYYY dates, optional **blank** Prior Day Occupancy row, then
Arrivals, Departures, Stayovers, Rooms on the books, Forecasted Pick up,
Forcasted occupied rooms and Occupancy % rows. Only the exact supported labels,
seven complete numeric cells per row and optional validated identity headers are
accepted. Duplicate/extra prose, ambiguous dates and missing cells fail closed.
Canonical and daily-table profiles reject supplied contradictory/duplicate
Hotel/Property code, ID or name headers.

The existing report has no printed hotel identifier. Headerless acceptance is
therefore an **explicit enrollment policy**, not independent proof of the paper's
hotel. A structurally identical wrong-hotel document without identifiers cannot
be independently distinguished. No hotel mapping comes from filename, inferred
capacity or arbitrary document prose. Production profile/sender/hotel enrollment
still needs approval and configuration. Code/schema migration never opts an old
tenant into the headerless profile automatically. An enrollment/profile change
while a job waits requires a fresh upload rather than silent reinterpretation.

The supplied original PDF was probed read-only, with the user's latest confirmed
292 total rooms: its layout, seven rows, totals and percentage checks pass using
a historical reference clock; it correctly fails the current-date age gate.
The original file was not changed, committed or uploaded. Automated fixtures use
fictional counts with equivalent formatting, not copied hotel forecast values.
Photos and scanned PDFs get a correction/resend response rather than automatic
OCR approval. The manual app's existing review path remains available. Reliable
unattended photo support requires separate measured extraction work.

## Development configuration and enrollment

Defaults in `.env.example` keep intake off and transport fake. The safe defaults
remain even if unrelated chat/OpenAI settings are present. No LLM is used here.
`WHATSAPP_ALLOWED_NUMBERS` and `WHATSAPP_MANAGER_MAP` belong to the old adapter and
**do not authorize this workflow**. Enrollment is not a public HTTP endpoint.

For a synthetic local experiment only, an operator explicitly calls:

```python
from services.whatsapp_store import WorkflowStore
from services.staff_settings import demo_defaults
s = WorkflowStore("/private/development/workflow.sqlite3")
s.enroll_tenant(tenant_id="synthetic-pilot", manager_id="synthetic-gm",
               hotel_code="SYNTHETIC-HOTEL", hotel_name="Synthetic Pilot Hotel",
               report_profile="canonical_v1")
s.enroll_sender(tenant_id="synthetic-pilot", phone_number_id="109876543210",
                sender="15555550123")
s.save_staff_settings("synthetic-gm", demo_defaults(), dataset_mode="demo")
# Same canonical store used by the app Parameters field, not enrollment:
s.save_hotel_parameters("synthetic-gm", {"room_capacity": 400})
```

The 400-room example above belongs only to the fictional end-to-end test.
The user's current built-in hotel demo uses the latest confirmed **Total rooms =
292**, editable in Parameters. Initialization applies only to the built-in demo
workbook, unlinked demo seed records and an absent inventory key. Existing explicit
numbers/cleared values, other workbooks, operational or unseeded records and
WhatsApp-linked tenants are not overwritten. Initialization has a revision guard
so a simultaneous user edit wins.

### One shared saved Parameters source

`settings.hotel_inventory.room_capacity` in the existing saved staff/settings
record is the sole room-inventory value. The same record's `cleaning_standards`
contains the shared checkout/stayover minutes. Parameters writes partial updates
atomically against the latest record, preserving newer staff availability. A
monotonic settings revision guards concurrent form saves and worker validation.
The worker reads this source before admission, retries if it changes before the
atomic calculation, and captures the exact settings/revision in the roster.
Missing inventory blocks unattended WhatsApp processing, not the manual roster
path; daily room-service counts and workbook task rows never fill it in.

Room capacity is no longer an enrollment argument. A legacy database column is
retained only for schema compatibility and is ignored. Other daily/session-only
Parameters keep their existing behavior; this does not claim every unrelated
parameter is now durably shared.

The numbers above are synthetic examples, not live destinations. Enrollment
inserts only and refuses implicit remapping. Disable sender/tenant enrollment
via an operator-reviewed database administration workflow. No self-enrollment.

Run FastAPI separately with `WHATSAPP_FORECAST_ENABLED=1` and a development
signature secret, then `python -m scripts.whatsapp_worker --once` (or explicit
local `--watch`). Fake transport has no media by default; use the complete demo
harness to register in-memory synthetic media. There is no arbitrary filesystem
media path accepted from a webhook. Runtime DB path is `COPILOT_STATE_DB`.
`WHATSAPP_PRIVATE_STORAGE` is a local private directory, never a public URL.

To inspect the synthetic app data, use the same development DB and explicit
`WEB_MANAGER_ID`, `WHATSAPP_DEV_INBOX_ENABLED=1`, and a true loopback browser
connection. No feature flag bypasses the non-loopback enrolled-manager guard.
The UI must not be proxied as authenticated production access.

## Durability, retry and idempotency semantics

- SQLite/private filesystem are **development adapters only**. Private objects
  are content-addressed within hashed tenant namespaces, chmod 0600, published
  atomically without clobbering; root is 0700. Workflow DB is chmod 0600.
- Message IDs deduplicate per business number. Content SHA-256 deduplicates
  repeated uploads by the same enrolled tenant/sender with unchanged saved
  inputs/result. Re-uploading the same file after saved Parameters/staff or manual
  roster changes creates a new version using current settings. A different sender's
  authorized upload is independent; no reply is ever redirected to a prior sender.
- Job claims have leases, bounded attempts and delayed backoff. Recovery can
  retry processing, but versions/outbox commit together. Older delayed forecasts
  cannot replace a newer WhatsApp forecast for the same hotel/week.
- Roster version allocation is atomic for both manual and WhatsApp saves.
- Before sending a document, enrollment and provider business-number binding are
  checked again. If saved settings or the latest roster changed, the worker sends
  correction/resend guidance instead of the obsolete PDF. A later resend is not
  mistaken for an already-delivered PDF when the earlier reply was only text.
- Outbox claims transition to `sending` **before** transport. Accepted messages
  are never blindly resent. Safe pre-send failures retry; uncertain outcomes,
  timeouts or crashed sends become `reconciliation_required`. Exactly-once
  delivery across Meta/network failures is **not claimed**.
- Signed, scoped status callbacks distinguish accepted/sent/delivered/read/failed.
  Known provider IDs reconcile delivery state. The optional opaque callback
  field has not been primary-source verified and is not sent by this adapter;
  lost-ack unknown provider IDs require manual reconciliation, never blind retry.
- Beyond 24 hours, an outbox becomes `outside_window` with
  `approved_template_required`; no template is sent or invented. Resending the
  identical file in a fresh service window can reopen that provably-unsent reply
  without another roster version when saved inputs/result are still current. It never reopens accepted/unknown sends.
- Final failed jobs/dead letters need operator investigation. No private raw
  provider errors, PDF text or secrets are stored in error fields.
- No automatic retention deletion is included; keep synthetic runtime directories
  private and remove them only under an operator-approved retention policy.

## Required before any real hotel data or live activation

The Meta adapter implements bounded media metadata/download, PDF upload and
messages endpoints, tested only through mock HTTP. Factory requires explicit
`WHATSAPP_PROVIDER=meta`, `WHATSAPP_PRODUCTION_APPROVED=1`, server-side token,
phone ID, version, app secret and verification token. **Even those variables
cannot enable live workflow with the development adapters**: worker construction
fails closed. See [verified API sources](whatsapp_api_sources.md).

Remaining gates, requiring explicit owner decisions/authorization:

1. Approve separate pilot number/account/provider setup, costs and Meta consent/
   policy obligations; choose test vs dedicated production registration.
2. Provision approved HTTPS webhook and separate supervised worker, secret
   manager, supported Graph API version, least-privilege Meta access and enrollment.
3. Replace SQLite/private local files with approved durable managed adapters,
   transaction/locking tests, tenant authorization and private encrypted storage.
4. Authenticate the app and map identity/role to tenant server-side; prohibit the
   unauthenticated public demo from sharing production data or fixed manager IDs.
5. Validate a labeled hotel report profile, actual saved staff/availability and
   hotel capacity; assess scheduling assumptions before operational use.
6. Isolate PDF processing with CPU/memory/time limits; add inbound rate limits,
   aggregate storage quotas, monitoring/dead letters, privacy/retention/backups,
   recovery drills and enrollment/credential rotation procedures.
7. Bind production worker credentials to the exact business-number ID; implement
   verified reconciliation and approved templates before outside-window messages.
8. Obtain explicit approval for a controlled real-number end-to-end test and
   real outbound messages. No live verification has happened in this change.

## Verification

```sh
pytest -q tests/test_whatsapp_forecast.py tests/test_whatsapp_provider.py \
          tests/test_whatsapp_workflow.py tests/test_whatsapp_app.py
pytest -q
python -m compileall -q api services scripts tests
git diff --check
```

Tests cover valid/invalid/native/scanned input, required dates/counts/capacity,
wrong hotel/sender/business ID, signature/body bounds, same-sender reply, hashes,
version/lock preservation, honest shortages, scoped delivery statuses, safe retry,
ambiguous send/crash, leases, stale arrivals, windows, recovery resend, privacy
permissions, concurrent claims/version allocation and public UI blocking.


### Existing Linux fixture limitation (not hidden or weakened)

The repository's pre-existing image fixture builder hard-codes macOS Arial paths.
On this Linux cloud workspace a raw full run therefore had 12 fixture setup errors
and one Arial-dependent OCR test failure. The existing external compatibility
setup uses Liberation Sans under `/tmp/doubletree-fonts/fonts/{Arial.ttf,Arial Bold.ttf}`
and `XDG_DATA_HOME=/tmp/doubletree-fonts`; no test assertions or original fixtures
were edited. With that same compatibility setup, the known pre-existing positive
OCR variants `[3]` and `[4]` still fail (missing OCR cells). They also failed in the
verified untouched baseline. The new native-PDF workflow never auto-accepts OCR.
See the final handoff for exact final pass/fail counts; a focused pass is not a
claim that the entire repository suite is green.
