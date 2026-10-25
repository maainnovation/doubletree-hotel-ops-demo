# Isolated Render synthetic WhatsApp pilot — deployment contract

This is the smallest bounded live-transport experiment, **not production hotel
onboarding**. It uses the existing scheduler and saved-settings schema, but a
separate fictional tenant and state volume. It does not synchronize or expose the
public Streamlit demo, modify main, accept real hotel reports or use real staff.
The actual hotel's editable 292-room Parameters remain unchanged.

## Infrastructure to approve

One Render Docker **web service**, one instance of `0.5c-512mb`, with a **1-GB
persistent disk** mounted at `/var/data`. Proposed name:
`hotel-whatsapp-synthetic-pilot`. The API and separate worker are supervised
processes in that same container; separate Render services cannot share this disk.
Auto-deploys and preview generation remain off. Deploy only a reviewed exact
feature-branch commit; no main merge or Streamlit update.

Official pricing checked 2026-10-25: $7/month compute + $0.25/month disk = about
**$7.25/month base**, plus applicable tax, usage overages and Twilio charges. This
is not a hard total bill cap. Account/workspace entitlement and checkout terms
must be inspected before creating the paid service. The template in
`deploy/render-pilot.yaml.example` is **not applied** and creates nothing by itself.

The container runs as UID/GID1000 with umask077. Its code is read-only to that
user. Startup requires a real mounted `/var/data` and dedicated private
`/var/data/whatsapp-pilot`; an ephemeral filesystem cannot enable live mode.
Check mounted ownership/writability after initial disabled deployment. If Render
mount ownership differs from the image, an authorized operator must correct only
that dedicated private directory; never use root runtime or chmod777 as a fix.

## Secret and configuration destination

Use the selected Render service's **Environment** configuration. No values go in
GitHub, browser code, logs or chat. Have the user enter the existing account secret
through the provider's private secret field, or authorize an explicitly supported
secret transfer—do not reveal/copy tokens from screenshots or browser pages.

| Name | Value / handling |
|---|---|
| `TWILIO_AUTH_TOKEN` | Existing Twilio account auth token; secret. Needed to verify webhook signatures and authenticate the scoped REST calls. No token is created by this code. |
| `TWILIO_ACCOUNT_SID` | Exact account SID for the approved Sandbox; private configuration. |
| `TWILIO_WHATSAPP_FROM` | Exact approved Sandbox WhatsApp business number; do not substitute a different trial number. |
| `PILOT_MANAGER_PHONE` | One explicitly confirmed sender's E.164 digits, without `+`; private personal configuration, never inferred from inbound traffic. |
| `PILOT_EXTERNAL_ORIGIN` | Assigned `https://<service>.onrender.com` origin only; no path/query. |
| `PILOT_WEEK_START` | Current week's Friday, `YYYY-MM-DD`, explicitly selected for the synthetic fixture. |
| `PILOT_APPROVED_UNTIL` | Explicit UTC expiry, at most 24 hours ahead; recommend a 2-hour test window. |
| `PILOT_DATA_ROOT` | `/var/data/whatsapp-pilot`. |
| `PILOT_VOLUME_CONFIRMED` | Keep `0` until the paid persistent volume and permissions are verified; then `1` only under approval. |
| `PILOT_ENABLED` | Keep `0` until hosting, secret/config, correct sender and the bounded real-message test are approved; then `1`. |

`PILOT_ENABLED=0` starts a disabled health endpoint and idle worker, without
constructing a provider or reading credentials. It does not enable the legacy
app's live flags. Reconfiguration changes require a Render restart/redeploy.
Existing pilot DB identity cannot be remapped or overwritten on restart.

## Exact external URLs and actions

Once the real origin is assigned and privately configured:

- Inbound Twilio Sandbox webhook: **POST**
  `<origin>/webhooks/twilio/whatsapp`.
- Outbound status callback: **POST** `<origin>/webhooks/twilio/status` (included in
  this adapter's outbound API request; account setup must permit callbacks).
- User downloads the tiny explicitly fictional PDF:
  `<origin>/synthetic-forecast.pdf`, then sends **that exact file** from the one
  enrolled WhatsApp account to the approved Sandbox number.
- Twilio retrieves the generated roster only through an unpredictable expiring
  capability at `<origin>/media/twilio/<token>/<filename>`; no directory listing,
  database download, staff endpoint, admin UI or normal hotel-app route exists.

Setting the inbound webhook is a persistent Twilio configuration change, not a
read-only test. It requires explicit destination/account approval. User join
success alone does not prove custom-PDF entitlement. Verify the supported legacy
Sandbox API route and costs before enabling any actual outbound attempt; do not
upgrade or reconnect to another trial number speculatively.

## Hard experiment limits

- Only the generated fixture's exact SHA-256 is accepted **before PDF parsing or
  source persistence**. Real/edited/unknown files are rejected; image/OCR paths
  cannot run. Download bounded to 1MiB, much larger than the tiny fixture.
- Fixed fictional staff/settings and canonical profile are checked at every
  operation. Changes revoke operation rather than admitting real data. The
  isolated synthetic saved capacity is 292; this is not a live-app settings sync.
- One enrolled account/business/sender combination; no auto enrollment.
- At most **10 unique queued messages** and **3 outbound API attempts** total,
  including corrections and safe retries, persisted across process restarts.
  Duplicate webhook IDs do not consume a new intake slot. Uncertain sends never
  auto-retry. These limits bound requests, not an exact currency amount.
- Expiry at most24h; all routes, retrieval and sends close when approval expires.
  **Expiry does not suspend/delete the Render service or stop hosting billing.**
  After the test, an explicitly authorized operator must suspend/delete the
  service and decide whether to retain/delete its synthetic volume and secret.
- PDF capability max10minutes/four GETs; HEAD supported; current state rechecked
  for every fetch. Uvicorn access logs disabled to keep bearer paths out of logs;
  verify the host/proxy's logging controls too. Twilio can retain its own copy.
- HTTPS inbound signature uses exact configured URL and every form field; no
  proxy-header signing. Bounded request body and 60requests/minute service limiter
  supplement, not replace, Render's DDoS controls.
- Twilio's documented `mms.twiliocdn.com` redirect is followed only once with a
  fresh **unauthenticated** client; other hosts/redirect chains fail closed.

## Readiness and acceptance checklist

1. Publish reviewed pilot code only when authorized; keep draft PR unmerged.
2. Connect/read the intended Render workspace; get explicit paid-service/disk
   and cost approval before creation. Initial deploy stays disabled.
3. Build image, verify pinned dependencies, port/health and private mount as
   UID1000. **The cloud coding environment has no Docker daemon, so an actual
   image build is still unverified until the approved host build.**
4. Configure approved private secret and exact mapping/origin/window. Verify
   Sandbox PDF entitlement/cost. Authorize the inbound webhook mutation and the
   bounded synthetic real-message test, then enable.
5. `/health` must return `live_test_ready:true`, not merely HTTP200/disabled. A
   fresh worker heartbeat is required. Do not expose normal app/API routes.
6. User sends the supplied fictional PDF. Confirm one inbound SID, matching
   account/sender, saved version, one outbound SID and actual WhatsApp PDF receipt
   or signed delivered status. Mock results do not substitute for this check.
7. Check that duplicate upload creates no extra reply; invalid file cannot reach
   parser; real hotel data remains prohibited. Stop at the bounded test result.
8. Disable pilot/webhook as authorized; agree service/volume/secret cleanup so
   hosting charges do not silently continue. Production rollout is a separate
   authenticated/durable deployment decision.

## Primary hosting references

- [Render pricing](https://render.com/pricing?trk=article-ssr-frontend-pulse_little-text-block)
- [Persistent disk constraints and encryption](https://render.com/docs/disks)
- [Blueprint specification](https://render.com/docs/blueprint-spec)
- [Docker on Render](https://render.com/docs/docker)
- [Web services and TLS](https://render.com/docs/web-services)

Local tests cover the runnable policy, remote-shaped signed requests/capability
retrieval, expiry, budgets, restart identity, symlink refusal and supervision with
mocked external HTTP. Actual account entitlement, host build/mount ownership,
network delivery and on-device receipt remain unverified until the approved test.

## Local verification on the reviewed revision

- Full suite with the existing external Linux font harness: **607 passed, 2 known
  baseline OCR failures**, 2 warnings (193.45s). No OCR fixtures were changed.
- This readiness increment adds **107 passing tests**: 45 CDN security cases,
  35 bounded-pilot cases and 27 supervisor/package cases.
- Independent security review findings were fixed and retested. Compileall,
  diff checks and current official Render Blueprint JSON Schema validation pass.
- A real local supervisor/API/worker process smoke test (disabled, no secrets)
  returned disabled health and shut down both children cleanly. This is not a
  Docker image build, Render deployment or live WhatsApp test.

## Observed legacy Sandbox compatibility: authenticated Media attestation

During the authorized synthetic live test, the signed inbound PDF webhook was
accepted, but the same-account Message-resource read returned HTTP 404 / Twilio
20404 and the filtered Message list was empty. The exact signed Media resource's
JSON metadata returned 200 with the expected account, parent Message SID, media
SID, PDF MIME type and fresh timezone-aware creation date. No send was attempted.

The isolated pilot now handles **only that typed Message-read 404** by validating
the authenticated Media metadata against its existing claimed, signed, enrolled
job. A mismatched Message, stale date, 401, redirect, malformed or truncated
response never triggers fallback. Media creation must be within 30 minutes,
not in the future, within five minutes of the immutable first webhook receipt,
and within the approved pilot interval. The original receipt and attestation
are audited on the job under its current claim; safe retries retain them.

Media creation is **not asserted to be WhatsApp message-arrival time**. For this
explicit fresh synthetic experiment only, a conservative window anchor ensures
outbound permission ends five minutes before the fixed pilot approval deadline,
not 24 hours after media creation. Normal provider/production Message timestamp
verification is unchanged. Exact fixture SHA, recipient, identity, expiry and
three-attempt limits remain unchanged. The Media metadata schema is documented
in [Twilio's official Media resource reference](https://www.twilio.com/docs/messaging/api/media-resource).

The compatibility fix adds **70 passing tests**. Final full regression on that
revision: **677 passed, 2 unchanged baseline OCR failures**, 2 warnings (198.24s).
Independent scoped security review passed, including receipt/anchor atomicity
and claim-safe retries. A read-only live media preflight confirmed the original
synthetic fixture's exact 1,677 bytes and SHA; outbound delivery is not implied.

## Redacted rejection diagnostics

The authorized recovery generated and saved the synthetic roster/PDF, but the
first outbound REST submission was rejected without a Message SID. The original
error handling intentionally retained only `provider_rejected`; no response body
can be recovered from that record. The Console Debugger upgrade gate is respected;
alternate APIs must not be used to bypass it, and no upgrade is performed.

Subsequent explicitly authorized attempts retain a validated numeric HTTP
status and optional numeric Twilio code, for example `twilio_http_403_code_21608`
(illustrative, not the observed error). Provider text, request/response bodies,
headers, URLs and credentials are never stored in this diagnostic. Bounded
malformed/truncated diagnostic bodies retain only the confirmed rejection status.
This does not automatically retry rejected messages or change the existing
ambiguous-send, rate-limit, service-window or persistent attempt-budget rules.

A later, explicitly authorized diagnostic also examines at most 2,048 characters
of the JSON `message` in memory and retains only fixed reason-category labels
and exact allowlisted parameter names. Unknown, negated, conditional or
conflicting phrases remain `unclassified`; these lossy classifications are not
proof of provider entitlement or a reason to change the sender or payload. The
raw prose, parameter values and other JSON fields are never persisted. HTTP
response/body limits and rejection/retry semantics are unchanged. This revision
does not schedule a retry: any operator retry requires separate approval, an
audited compare-and-set of the already-saved outbox and the original durable
three-attempt cap. There is no fourth attempt or automatic trial upgrade.

## Optional, operator-approved download probe

The isolated pilot supports an explicitly created, **one-download / ten-minute**
capability to its already-generated fictional PDF for HTTPS troubleshooting.
This is disabled by default (no diagnostic grant); there is no public mint or
admin endpoint. Only the in-process operator method can create a probe, and only
for the exact terminal failed synthetic document with the unchanged exhausted
three-attempt budget. It does not requeue a message, change delivery state, make
a provider call or reset any limit.

Probe grants occupy a separate `pilot_media_probes` table and store only token
hashes. The normal `/media/twilio/{token}/{filename}` GET/HEAD route and its PDF
response/header code are reused. Every access revalidates the saved roster,
enrollment, current settings, file hash, approval and customer-service window.
HEAD does not consume the single GET. Expiry is the earliest of ten minutes,
pilot approval expiry and the service window. Successful GET or explicit
revocation makes the link unavailable; the non-secret audit row remains, and
this outbox cannot silently mint another probe. Ordinary delivery grants retain
their original status checks and limits.

An authorized operator must keep the capability only in memory, use HTTPS with
redirects disabled, compare the downloaded bytes to the private saved PDF,
explicitly revoke in a `finally` block and verify a subsequent HEAD is 404.
Print only safe status/header checks and hash-equality results, never the URL,
raw response body or credentials. A request from the Render shell through the
public hostname verifies that public HTTPS edge path, but is **not** a test from
an independent network or proof that Twilio itself can download it.
