# Owner-only PDF/photo and private-app test

This is a separate, disabled-by-default runtime for the existing private Render
service. It is not the public Streamlit app and does not activate partners or
clients. The approved epoch is **10 days from explicit activation, 500 total
outgoing attempts, no automatic send retries, and a monitored $10 budget**.
An acknowledgment, review prompt, correction or PDF each consumes one attempt.
A report receives at most two attempts: ACK/review/correction, then final PDF or
error. Uncertain sends require operator reconciliation, never blind retries.

## Data and calculation boundary

- Keep the existing owner sender, business sender, account, private database and
  immutable legacy identity. No inferred hotel mapping or new enrollment.
- Explicitly enable the known `hotel_daily_table_v1` profile for this owner.
  It accepts the observed named rows and unambiguous M/D/YYYY dates, but requires
  a complete consecutive week and all counts. Current date windows stay enforced.
- The approved source policy is `redacted_owner_reports`: no guest details;
  files are private and processed by bounded local OCR, not an external AI API.
- Native supported PDFs may calculate after validation. Photos and scans always
  require a sender-bound, expiring confirmation of all extracted dates/counts.
  Unclear/inconsistent input does not create a roster. No silent re-dating.
- Private app uploads always require review and **never send WhatsApp messages**.
  They use the exact same persisted Parameters, staff, locks and roster versions
  as WhatsApp. Demo staff remain fictional; review shortages before operations.
- Capacity is the existing saved Parameters value (292 initially), not a second
  enrollment value. Inspection minutes are not included in weekly staffing
  demand; all stayovers count. The UI/PDF disclose these limitations.
- Sources and result PDFs share a 512 MiB private object quota and a 128 MiB
  minimum free-disk reserve. No silent deletion or retention promise is made.
  A full quota blocks writes and requires an operator cleanup decision.

## Cost is monitored, not an account-wide hard cap

The worker polls its own Twilio account Usage Records `totalprice`, using only
existing credentials, every 60 seconds. An operator establishes the explicit
activation baseline. Missing baseline, non-USD/malformed responses, provider
errors, or source/cache timestamps older than 300 seconds deny further sends.
Reported spend never decreases the held maximum delta. The gate additionally
holds a lifetime **$0.10 safety reserve per observed inbound and outbound
attempt**, and stops projected usage at **$8**, leaving $2 headroom.

The $0.10 amount is a conservative policy, **not a verified maximum tariff**.
The double-counted reserve can stop the test well before 500 replies. Incoming
messages and other account activity can still incur provider charges even if
this application rejects input or stops replying. Usage data can lag; the app
cannot promise an exact $10 account cap. No auto-recharge or purchase is enabled.

## Dependency privacy gate

PyMuPDF2 may import its optional layout/ONNX dependency even for fixture setup.
ONNX Runtime1.31 Linux builds include default-on Microsoft1DS telemetry.
This application forces **`ORT_DISABLE_TELEMETRY=1` before import** in service
and script startup, Docker and the sanitized media-child environment. Test
commands must set the same variable before Python starts. Calling the Python
telemetry-disable API after initialization is insufficient. An already-loaded
unprotected ONNX process is refused and must restart; no telemetry endpoint is
proxied, redirected or allowed through a security block.

A bounded native-import verification used a kernel kill-on-network rule: its
negative-control socket call was killed, while a12-second opted-out PyMuPDF/ONNX
import and fictional native-PDF operation completed without a network syscall
attempt. This is bounded evidence, not a lifetime network-capture claim.

The source is Microsoft's [official privacy documentation](https://github.com/microsoft/onnxruntime/blob/main/docs/Privacy.md).
The isolated OCR child additionally denies network syscalls. This gate does not
claim any prior payload was or was not transmitted; no such payload was captured.

## Disabled staging: exact sequence

1. Confirm current deployed head and no pending sends. Save a private SQLite
   online backup and integrity/hash evidence before approval/migration changes.
   Preserve every old job, roster and attempt ledger. Do not restore or delete
   data automatically.
2. In the existing service set **`PILOT_ENABLED=0`** and
   **`PILOT_REVIEW_ENABLED=0`**, **`PILOT_REVIEW_WHATSAPP_ENABLED=0`**
   and **`ORT_DISABLE_TELEMETRY=1`**. Save/deploy and verify disabled health before
   changing the code branch/image. Existing masked credentials stay untouched.
3. Deploy the verified `feature/forecast-input-validation` head using
   **`Dockerfile.review-pilot`**, context `.`, health `/health`, one instance,
   existing 512 MiB/0.5 CPU plan and existing `/var/data` disk. Keep auto-deploy
   off and the unchanged `PILOT_DATA_ROOT=/var/data/whatsapp-pilot`.
   Do not accept the Docker image's fallback data-root as a new state store.
4. Verify build `pip check`, Tesseract 5.5.0 English, libseccomp, nonroot UID1000,
   source tree non-writability, private-child0700, disk availability and disabled
   `/health`. Record resolved package versions; no live activation yet.
5. Run `python -m scripts.measure_review_runtime --run-synthetic --mode attached-disabled`.
   Require all native/scanned/photo cases, real cgroup/CPU counters, >=64 MiB
   aggregate kernel-peak margin, no OOM increment, service PID continuity and
   free-space gates. A local summed-RSS result is not a substitute. If it fails,
   stop; do not upgrade hosting or weaken input/resource guards without approval.
6. With explicit credential-creation approval, the owner or operator enters a
   unique **32–512 ASCII, no-whitespace** access key directly in Render as `PILOT_REVIEW_APP_KEY`; never paste it into chat, logs or GitHub.
   This grants only the existing owner hotel and expires with the ten-day epoch.
   Set `PILOT_REVIEW_RESOURCE_CONFIRMED=1` only after the measured stage passes.
7. Use the operator-only `scripts.review_pilot_operator` procedure to create a
   fresh explicit approval ID and initialize its own-account usage baseline,
   with both intake flags still0. If API evidence is stale/unavailable, remain
   disabled; never invent a zero baseline or extend expiry silently.
8. Set `PILOT_REVIEW_APPROVAL_ID` to that exact epoch and
   `PILOT_REVIEW_ENABLED=1`; keep `PILOT_ENABLED=0` and
   **`PILOT_REVIEW_WHATSAPP_ENABLED=0`** for private-app-only QA. Preserve all existing legacy
   environment values, including original `PILOT_APPROVED_UNTIL` and week.
9. Verify `status=private_app_only`, private app readiness, worker heartbeat
   and cgroup. WhatsApp intake and dispatch remain paused, including already
   queued jobs; the provider reservation gate also denies sends in this mode.
   Browser-check `/private` login/logout, expired/wrong key, saved settings and
   staff, upload/review/cancel/back/regenerate, roster/PDF download and mobile
   layout. Do not call unperformed browser checks passed.
10. After private browser QA and a fresh cost check, explicitly set
    **`PILOT_REVIEW_WHATSAPP_ENABLED=1`**, deploy, and verify full readiness.
    Only then give the owner one current fictional PDF/photo and exact CONFIRM
    instructions. Observe signed intake, no premature photo calculation, one
    reviewed version, and accepted/delivered/read receipts. A send failure
    remains terminal. Do not enroll partners or send them instructions yet.

Operator commands (on the disabled host; no credential values in arguments):

```sh
python -m scripts.review_pilot_operator backup
python -m scripts.review_pilot_operator activate --approval-id owner-review-20261026
# Only if that activation reports an unavailable usage baseline:
python -m scripts.review_pilot_operator baseline --approval-id owner-review-20261026
# Emergency durable revocation while both intake flags remain0:
python -m scripts.review_pilot_operator revoke --approval-id owner-review-20261026
```

Activation itself creates a consistent private backup before its additive changes.
`approved_baseline_ready_disabled` is not permission to skip resource/browser
checks. `approved_baseline_missing_disabled` must remain disabled. Repeating
baseline never resets a recorded baseline or extends the approval. Repeating
activation is denied; do not create another ID to bypass that protection.
The ID above is an operator label, not an access credential.

## Configuration contract

New variables (all access defaults deny):

| Variable | Value/purpose |
|---|---|
| `ORT_DISABLE_TELEMETRY` | Must be`1` before process startup; Docker forces default |
| `PILOT_REVIEW_ENABLED` | `0` during stage; `1` for approved private-app epoch |
| `PILOT_REVIEW_WHATSAPP_ENABLED` | Defaults`0`; separate intake/dispatch gate,`1` only after private app QA |
| `PILOT_REVIEW_RESOURCE_CONFIRMED` | `1` only after measured512MiB gate |
| `PILOT_REVIEW_APPROVAL_ID` | Explicit existing durable owner approval |
| `PILOT_REVIEW_APP_KEY` | Explicitly authorized private key; never printed |

Reuse existing Twilio account/token/from, owner number, external HTTPS origin,
confirmed private volume and old identity/week/expiry without revealing values.
Webhook/status/media routes stay unchanged. The owner review runtime is the
only permitted live OCR adapter; generic live review remains blocked.

## Stop and rollback

Set `PILOT_REVIEW_WHATSAPP_ENABLED=0` and `PILOT_REVIEW_ENABLED=0`,
keep legacy `PILOT_ENABLED=0`, redeploy and verify
private APIs deny access and webhook intake is disabled. Pending provider
receipts may need read-only reconciliation; no retry is authorized by rollback.
The old `b94341f` code/image remains available, but **code rollback does not undo
schema/data changes**. New tables are additive. Keep backups and newer data;
restoring a snapshot would discard later work and requires a separate decision.
No automatic expiry extension, old attempt reset or requeue is part of recovery.

Sandbox participation may require rejoining after three days; free-form replies
still require the provider-verified 24-hour customer-service window. Partner or
client testing requires explicit new identity/hotel enrollment and access scope.

## Primary references

- [Twilio Usage Records](https://www.twilio.com/docs/usage/api/usage-record)
- [WhatsApp pricing](https://www.twilio.com/en-us/whatsapp/pricing)
- [Incoming WhatsApp messages cannot be blocked by the application](https://help.twilio.com/articles/360058712633-Can-I-reject-or-block-incoming-messages-to-my-WhatsApp-number-on-Twilio)
- [Sandbox participation and testing](https://www.twilio.com/docs/whatsapp/sandbox)
- [Webhook signature verification](https://www.twilio.com/docs/usage/webhooks/webhooks-security)

See `review_pilot_resources.md` for the resource test's limits and exact gates.
