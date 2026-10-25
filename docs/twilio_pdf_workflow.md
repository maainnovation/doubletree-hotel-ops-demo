# Twilio WhatsApp PDF adapter — synthetic implementation, live disabled

Verified against official Twilio documentation on 2026-10-25. This is an
alternative transport for the existing forecast/roster workflow, not a second
scheduler or a new Parameters source. The user's built-in demo inventory stays
**292**, editable in Parameters. No real credentials, account setup, hosting,
messages or account-specific PDF entitlement were used to implement this adapter.

## What is implemented

1. `POST /webhooks/twilio/whatsapp` accepts bounded form-encoded requests and uses
   the official Twilio SDK to check `X-Twilio-Signature` against **every decoded
   form parameter and the exact configured external HTTPS URL**. Duplicate keys,
   malformed encoding, wrong URL query/path, JSON and oversized forms fail closed.
   Host/Forwarded headers do not construct the signed URL. Return empty TwiML
   promptly; no download, extraction, scheduling or outbound send runs here.
2. Exact AccountSid + WhatsApp business number + sender enrollment determines
   one hotel. Twilio business identities are namespaced
   `twilio:AC<32 hex>:<business E.164 digits>` and cannot match Meta enrollment.
   MediaUrl0 must bind that account and signed MessageSid. Unknown identities
   cause no media fetch or reply. Existing explicit report profiles still apply.
3. The separate durable worker verifies the Message resource's account, SID,
   direction, sender, recipient and original creation time. **Webhook receipt
   time does not establish the 24-hour window**: signatures have no timestamp and
   can be replayed. Old/mismatched messages fail without sending. Message SID
   uniqueness suppresses replay; content hashes suppress unchanged duplicates.
4. Fetch media with authenticated, bounded HTTPS and exact MIME. Only canonical
   own-account Twilio API media URLs are permitted; redirects are rejected, not
   followed with credentials. Twilio supplies no signed media checksum, so the
   source hash is computed and retained after download rather than invented.
5. Reuse strict native-PDF validation, latest shared saved Parameters/staff,
   versioned roster history, locks/manual assignments and shortage reporting.
   Scans/photos are still correction/manual-review cases, not unattended OCR.
6. Publish only the exact current outbox PDF through a private-store capability:
   random 256-bit token, only its hash persisted, at most 10 minutes and four GETs
   by default, bounded by the remaining service window. HEAD works without
   consuming a GET. Each access rechecks tenant, enrollment/profile, synthetic
   saved data, current roster/settings, outbox state and PDF integrity. Safe
   retry rotates the token; revocation/stale settings/expiry deny retrieval.
7. POST form `From`, `To`, `MediaUrl` and optional `StatusCallback` to the official
   Messages endpoint. Document `Body` is intentionally omitted: Twilio documents
   that it is ignored for WhatsApp PDF messages. Calculations and warnings are
   inside the PDF itself. Filename is the bounded `roster-YYYYMMDD.pdf`.
8. Signed `/webhooks/twilio/status` callbacks reconcile known returned message
   SIDs. API acceptance is not delivery. Unknown/lost-acknowledgement SIDs still
   need operator investigation. Timeouts/ambiguous sends never auto-retry.

## Reproducible no-network exercise

```bash
python -m scripts.demo_twilio_workflow --output /tmp/twilio-pdf-demo
python -m pytest -q tests/test_twilio_provider.py tests/test_twilio_media.py tests/test_twilio_workflow.py
```

The demo uses synthetic dates/staff/counts and a separate fictional **400-room**
hotel, so it can reuse the existing synthetic PDF fixture without altering the
user's 292-room setting. All Twilio HTTP is an explicit `httpx.MockTransport`.
The HTTP callback and PDF HEAD/GET requests are in-process FastAPI TestClient
calls; no socket, external provider, credential or real message is involved.
A PDF and non-sensitive summary are written only to the requested new folder.

## Configuration and deliberate live blockers

All defaults remain off: `WHATSAPP_PROVIDER=fake`,
`WHATSAPP_FORECAST_ENABLED=0`, `WHATSAPP_PRODUCTION_APPROVED=0`,
`TWILIO_WEBHOOK_ENABLED=0`, `TWILIO_DEV_MEDIA_ENABLED=0`.
The `.env.example` provides empty server-side placeholders only. Never put auth
values in browser code, commit them, or log webhook bodies/URLs/media tokens.

- Live provider construction requires explicit approval; the current worker
  rejects all live providers with development storage. Selecting `twilio` from
  environment also requires a reviewed **production** private-media publisher.
  No such production adapters or deployment are included.
- The included media route is **loopback-only and synthetic-only**, disabled by
  default. It cannot serve Twilio over the internet as shipped. Do not reverse
  proxy this development endpoint publicly or remove the gate to "make it work".
  Production needs approved HTTPS private object storage/routing, token-safe
  access logs (redact the full media path), quota/rate limits and retention.
  Bearer links are access-limited, not proof of recipient identity; anyone with
  a leaked valid capability can fetch during its allowed lifetime. Twilio must
  fetch the PDF, and its service may retain its own media copy: that requires an
  explicit privacy/retention decision before real hotel data.
- Actual webhook/sender/profile enrollment, authorized recipient opt-in, approved
  hosted worker/database/app authentication, secret management, document
  isolation/resource limits and cost approval remain prerequisites.
- Current download policy rejects redirects. If a real Twilio API media URL
  redirects to its CDN, downloading will fail closed until the actual documented
  host/path/credential behavior is reviewed; never enable arbitrary redirects.
- No templates are configured. Outside 24 hours, the outbox stops rather than
  issuing a free-form PDF or speculative template. Failed/ambiguous media sends
  need review; this implementation does not assert transport idempotency.

## Sandbox versus current trial

Twilio's general WhatsApp API supports PDF media. Its current **Try out WhatsApp**
trial flow is a different restricted product: only provided preset ContentSid
messages are allowed, and it does not support custom media/TwiML. Legacy Sandbox
availability/join success is not proof that this account may send custom PDF for
free or without upgrade. Account-specific entitlement and cost remain unverified;
a controlled live test requires separate approval after those checks. This code
implements the documented generic API, not a bypass of trial restrictions.

## Primary sources

- [Twilio WhatsApp media tutorial](https://www.twilio.com/docs/whatsapp/tutorial/send-and-receive-media-messages-whatsapp-python):
  PDF support, inbound `MediaUrl0`/`MediaContentType0`, outbound media URL and
  document-text limitations.
- [Secure webhooks](https://www.twilio.com/docs/usage/webhooks/webhooks-security):
  SDK signing, account token, configured URL and all request parameters.
- [Message resource](https://www.twilio.com/docs/messaging/api/message-resource):
  create/fetch message contract, account and identity fields, creation time,
  statuses and callbacks.
- [Media resource](https://www.twilio.com/docs/messaging/api/media-resource):
  account/message-scoped media URLs and authenticated retrieval.
- [Accepted media types](https://www.twilio.com/docs/messaging/guides/accepted-mime-types):
  `application/pdf`, GET/HEAD checks, headers and short filenames. Documentation
  pages differ on upper WhatsApp media limits; this implementation keeps the
  stricter existing **10 MiB** PDF limit (below both cited limits).
- [Try out WhatsApp](https://www.twilio.com/docs/usage/trials/try-out-whatsapp) and
  [WhatsApp Sandbox](https://www.twilio.com/docs/whatsapp/sandbox): current trial
  restrictions and the separate legacy Sandbox path.

## Verification notes

The provider-scoped queue also keeps the default unscoped fake worker away from
Twilio jobs/outbox, including expired-lease recovery. Direct operator store claims
remain generic. The existing mismatched-provider test was intentionally updated
from expecting destruction of foreign work to expecting it remain pending, with
no send or roster change; new mixed-provider tests cover both directions. Existing
OCR fixtures and their known Linux baseline failures were not changed.

A status callback can arrive before the outbound API acknowledgement's SID has
been persisted. Such an unknown callback is ignored; a later known callback or
operator reconciliation is needed. This does not turn API acceptance into delivery
or justify a blind resend.
