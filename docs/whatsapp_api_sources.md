# WhatsApp transport references and limits

Verified 2026-10-25 against primary Meta/WhatsApp material. No live API request,
account setup, credentials, or real outbound message was used for verification.

- [Meta's official Cloud API collection](https://www.postman.com/meta/whatsapp-business-platform/collection/wlk6lh4/whatsapp-cloud-api)
  documents bearer-token authorization. Select an explicitly supported Graph API
  version at deployment; the code has no assumed default version.
- [Retrieve Media URL](https://www.postman.com/meta/whatsapp-business-platform/request/ptjyi84/retrieve-media-url)
  specifies `GET /{version}/{media-id}?phone_number_id=...`. The adapter binds the
  lookup to the configured business number, then downloads the returned HTTPS URL.
- [WhatsApp's media-message example](https://whatsappbusiness.com/blog/media-messages-via-app/)
  documents multipart upload through `/{phone-number-id}/media`, including the
  file, MIME type and messaging product. Its historical sample version is not
  used as a current version recommendation.
- [Send Document Message by ID](https://www.postman.com/meta/whatsapp-business-platform/request/zjtbdpz/send-document-message-by-id)
  documents posting a document object to `/{phone-number-id}/messages` and the
  returned message identifier. An API acknowledgement does not prove delivery.
- [WhatsApp Business Messaging Policy](https://whatsappbusiness.com/policy/?faq=5)
  requires approved templates outside the rolling 24-hour customer-service
  window. The workflow must enforce this before invoking this transport; this
  first version does not send templates.

Direct Meta developer-reference pages returned HTTP 429 during research. The
accessible official Meta collection and WhatsApp publication supplied the request
contracts above; the new adapter has been validated using mocked HTTP, **not** a
live business account.

## Deliberately narrower pilot behavior

Only enrolled international phone-number recipients and PDF/JPEG/PNG inbound
media are supported. The development byte limit is 10 MiB. Download URLs must be
HTTPS, use `lookaside.fbsbx.com` and the `/whatsapp_business/attachments/` path,
and cannot redirect. This is an intentionally narrow security allowlist, not a
promise that every future Meta CDN URL will match; review a changed official URL
contract before expanding it. The worker separately checks webhook SHA-256 and
file contents. Outbound PDFs are uploaded privately by ID, never via public links.

`WHATSAPP_PROVIDER=fake` is the default and never contacts Meta. Live transport
requires `WHATSAPP_PROVIDER=meta`, `WHATSAPP_PRODUCTION_APPROVED=1`, a token,
business phone number ID, explicitly selected Graph version, app secret and
verification token. These flags are configuration gates, not production approval
or proof that durable/authenticated deployment prerequisites have been met.

The fake adapter's idempotency keys deduplicate in memory for synthetic tests.
They do **not** imply equivalent Meta guarantees. The live adapter never retries
internally. Pre-connection/fetch/upload failures can be retried before a message
is sent. Send timeouts, write/read errors, server errors and unreadable success
acknowledgements raise `AmbiguousSendError`: stop and reconcile, do not resend.
Provider response bodies, tokens and download URLs are excluded from exceptions.

**Lost-acknowledgement limitation:** the live adapter does not send
`biz_opaque_callback_data`. Its current contract could not be independently
verified in the accessible primary references above (Meta's direct references
were unavailable), so an optional unverified field was not added. Status events
can reconcile known returned message IDs; an acknowledgement lost before its ID
is saved may require operator investigation and cannot be advertised as automatic
reconciliation. Keep that outbox entry blocked rather than re-sending it.

## Alternative Twilio adapter

The separate [Twilio PDF guide](twilio_pdf_workflow.md) records current official
API/security sources, synthetic verification and account-specific trial limits.
Meta remains intact; neither live adapter is enabled by default.
