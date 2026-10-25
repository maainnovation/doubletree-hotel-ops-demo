"""WhatsApp transport boundary. Fake by default; no networking during webhook intake.

The caller owns enrollment, customer-service-window checks, durable outbox claims,
and reconciliation. A Meta API acknowledgement is not proof of delivery. Meta's
messages endpoint does not provide an idempotency guarantee for our local key:
an uncertain send MUST NOT be retried automatically.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
import re
from typing import ClassVar, Mapping, Protocol
from urllib.parse import urlsplit

import httpx


ALLOWED_MIME_TYPES = frozenset({"application/pdf", "image/jpeg", "image/png"})
MAX_OUTBOUND_PDF_BYTES = 10 * 1024 * 1024
_JSON_LIMIT = 64 * 1024


class ProviderError(RuntimeError):
    """Permanent/configuration failure; messages deliberately contain no raw responses."""


class RetryableProviderError(ProviderError):
    """Safe to retry: no user-visible message could have been accepted."""


class AmbiguousSendError(ProviderError):
    """A send may have been accepted; require reconciliation, NEVER blind retry."""


class WhatsAppProvider(Protocol):
    is_live: bool
    def fetch_media(self, media_id: str, expected_mime: str, max_bytes: int) -> bytes: ...
    def send_text(self, to: str, text: str, idempotency_key: str) -> str: ...
    def send_document(self, to: str, pdf_bytes: bytes, filename: str,
                      caption: str, idempotency_key: str) -> str: ...


def _recipient_and_key(to: str, key: str) -> None:
    if not isinstance(to, str) or not re.fullmatch(r"[1-9][0-9]{6,14}", to):
        raise ProviderError("Recipient must be an explicitly enrolled international phone number.")
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key):
        raise ProviderError("A bounded outbox idempotency key is required.")


def _text_payload(to: str, text: str, key: str) -> dict:
    _recipient_and_key(to, key)
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise ProviderError("Text must contain between 1 and 4000 characters.")
    return {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}


def _validate_pdf(to: str, pdf: bytes, filename: str, caption: str, key: str) -> None:
    _recipient_and_key(to, key)
    if not isinstance(pdf, bytes) or not pdf.startswith(b"%PDF-") or len(pdf) > MAX_OUTBOUND_PDF_BYTES:
        raise ProviderError("Outbound document must be a PDF within the configured size limit.")
    if not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}\.pdf", filename):
        raise ProviderError("A safe PDF filename is required.")
    if not isinstance(caption, str) or len(caption) > 1024:
        raise ProviderError("Document caption must be at most 1024 characters.")


def _media_limits(expected_mime: str, max_bytes: int) -> None:
    if expected_mime not in ALLOWED_MIME_TYPES:
        raise ProviderError("Unsupported inbound media type.")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 0 < max_bytes <= 10 * 1024 * 1024:
        raise ProviderError("Invalid inbound media byte limit.")


@dataclass
class FakeWhatsAppProvider:
    """In-memory synthetic adapter. No external messages, files, or credentials.

    Media values may be bytes (using the caller's expected MIME type), or
    ``(bytes, mime_type)`` pairs to test mismatches. Fake key deduplication is a
    test convenience, NOT evidence that the Meta transport is idempotent.
    """
    is_live: ClassVar[bool] = False
    media: dict[str, bytes | tuple[bytes, str]] = field(default_factory=dict)
    sent: list[dict] = field(default_factory=list)
    _keys: dict[str, tuple[str, str]] = field(default_factory=dict, repr=False)

    def fetch_media(self, media_id: str, expected_mime: str, max_bytes: int) -> bytes:
        _media_limits(expected_mime, max_bytes)
        if media_id not in self.media:
            raise ProviderError("Synthetic media was not registered.")
        value = self.media[media_id]
        content, mime = value if isinstance(value, tuple) else (value, expected_mime)
        if mime != expected_mime or not isinstance(content, bytes) or not content or len(content) > max_bytes:
            raise ProviderError("Synthetic media type or size is invalid.")
        return content

    def _record(self, payload: dict, key: str) -> str:
        canonical = {k: hashlib.sha256(v).hexdigest() if isinstance(v, bytes) else v for k, v in payload.items()}
        fingerprint = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()
        if key in self._keys:
            old_fingerprint, provider_id = self._keys[key]
            if fingerprint != old_fingerprint:
                raise ProviderError("Outbox key reused for a different payload.")
            return provider_id
        provider_id = "fake." + hashlib.sha256(key.encode()).hexdigest()[:24]
        self._keys[key] = (fingerprint, provider_id)
        self.sent.append({**payload, "idempotency_key": key, "provider_id": provider_id})
        return provider_id

    def send_text(self, to: str, text: str, idempotency_key: str) -> str:
        _text_payload(to, text, idempotency_key)
        return self._record({"type": "text", "to": to, "text": text}, idempotency_key)

    def send_document(self, to: str, pdf_bytes: bytes, filename: str,
                      caption: str, idempotency_key: str) -> str:
        _validate_pdf(to, pdf_bytes, filename, caption, idempotency_key)
        return self._record({"type": "document", "to": to, "pdf_bytes": pdf_bytes,
                             "filename": filename, "caption": caption}, idempotency_key)


class MetaWhatsAppProvider:
    """Gated official Cloud API transport; does not claim production readiness.

    ``production_approved`` is an operator-controlled deployment gate, not a
    replacement for authorization, durable storage, or a security review.
    ``transport`` exists for hermetic httpx.MockTransport tests only.
    """
    is_live = True

    def __init__(self, access_token: str, phone_number_id: str, api_version: str, *,
                 production_approved: bool = False, transport: httpx.BaseTransport | None = None):
        if production_approved is not True:
            raise ProviderError("Live WhatsApp transport is disabled pending deployment approval.")
        if not access_token or any(c.isspace() for c in access_token):
            raise ProviderError("A valid server-side access token is required.")
        if not re.fullmatch(r"[0-9]{1,40}", phone_number_id or ""):
            raise ProviderError("A numeric business phone number ID is required.")
        if not re.fullmatch(r"v[0-9]{1,3}\.0", api_version or ""):
            raise ProviderError("An explicit supported Graph API version is required.")
        self._token = access_token
        self.phone_number_id = phone_number_id
        self._base = f"https://graph.facebook.com/{api_version}"
        self._transport = transport

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=httpx.Timeout(20, connect=5), follow_redirects=False,
                            trust_env=False, transport=self._transport,
                            headers={"Authorization": f"Bearer {self._token}", "Accept-Encoding": "identity"})

    @staticmethod
    def _read(response: httpx.Response, limit: int) -> bytes:
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise ProviderError("Encoded provider responses are not accepted.")
        length = response.headers.get("content-length")
        if length:
            try:
                if int(length) < 0 or int(length) > limit:
                    raise ProviderError("Provider response exceeds the byte limit.")
            except ValueError:
                raise ProviderError("Invalid provider response length.") from None
        chunks, total = [], 0
        for chunk in response.iter_bytes(chunk_size=65536):
            total += len(chunk)
            if total > limit:
                raise ProviderError("Provider response exceeds the byte limit.")
            chunks.append(chunk)
        return b"".join(chunks)

    def _json_request(self, method: str, url: str, *, sending: bool = False, **kwargs) -> dict:
        try:
            with self._client() as client, client.stream(method, url, **kwargs) as response:
                if sending and (response.status_code >= 500 or response.status_code == 408):
                    raise AmbiguousSendError("Meta send outcome is unknown; reconciliation required.")
                if response.status_code == 429 or (not sending and response.status_code >= 500):
                    raise RetryableProviderError("Provider temporarily unavailable before a confirmed send.")
                if not 200 <= response.status_code < 300:
                    raise ProviderError("Provider rejected the request; review configuration or media.")
                try:
                    data = json.loads(self._read(response, _JSON_LIMIT))
                    if not isinstance(data, dict):
                        raise ValueError
                    return data
                except (ValueError, ProviderError):
                    if sending:
                        raise AmbiguousSendError("Meta send acknowledgement unreadable; reconciliation required.") from None
                    raise ProviderError("Invalid provider metadata response.") from None
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise RetryableProviderError("Provider connection failed before sending.") from None
        except httpx.RequestError:
            if sending:
                raise AmbiguousSendError("Meta send outcome is unknown; reconciliation required.") from None
            raise RetryableProviderError("Provider transfer interrupted before a user-visible send.") from None

    def fetch_media(self, media_id: str, expected_mime: str, max_bytes: int) -> bytes:
        _media_limits(expected_mime, max_bytes)
        if not isinstance(media_id, str) or not re.fullmatch(r"[0-9]{1,40}", media_id):
            raise ProviderError("Invalid Meta media ID.")
        metadata = self._json_request("GET", f"{self._base}/{media_id}",
                                      params={"phone_number_id": self.phone_number_id})
        if str(metadata.get("id", "")) != media_id or metadata.get("mime_type") != expected_mime:
            raise ProviderError("Provider media identity or MIME type did not match the webhook.")
        size = metadata.get("file_size")
        try:
            if isinstance(size, bool) or not 0 < int(size) <= max_bytes or str(int(size)) != str(size):
                raise ValueError
        except (TypeError, ValueError):
            raise ProviderError("Provider media size is missing or exceeds the byte limit.") from None
        url = metadata.get("url")
        try:
            parsed = urlsplit(url) if isinstance(url, str) else None
            if (not parsed or parsed.scheme != "https" or parsed.hostname != "lookaside.fbsbx.com"
                    or parsed.port not in (None, 443) or parsed.username or parsed.password or parsed.fragment
                    or not parsed.path.startswith("/whatsapp_business/attachments/")):
                raise ValueError
        except ValueError:
            raise ProviderError("Provider media URL is not an approved HTTPS Meta download endpoint.") from None
        try:
            with self._client() as client, client.stream("GET", url) as response:
                if response.status_code == 429 or response.status_code >= 500:
                    raise RetryableProviderError("Media download temporarily unavailable.")
                if not 200 <= response.status_code < 300:
                    raise ProviderError("Media download rejected or redirected.")
                if response.headers.get("content-type", "").split(";", 1)[0].strip() != expected_mime:
                    raise ProviderError("Downloaded media MIME type does not match the webhook.")
                content = self._read(response, max_bytes)
        except httpx.RequestError:
            raise RetryableProviderError("Media download interrupted; safe to retry.") from None
        if len(content) != int(size):
            raise ProviderError("Downloaded media size does not match provider metadata.")
        return content

    def _send(self, payload: dict, idempotency_key: str) -> str:
        # The key belongs to the durable outbox; do NOT invent an unsupported
        # Idempotency-Key guarantee or retry uncertain sends in this adapter.
        response = self._json_request("POST", f"{self._base}/{self.phone_number_id}/messages",
                                      sending=True, json=payload)
        messages = response.get("messages")
        if (not isinstance(messages, list) or not messages or not isinstance(messages[0], dict)
                or not isinstance(messages[0].get("id"), str) or not messages[0]["id"].startswith("wamid.")):
            raise AmbiguousSendError("Meta returned no usable message ID; reconciliation required.")
        return messages[0]["id"]

    def send_text(self, to: str, text: str, idempotency_key: str) -> str:
        return self._send(_text_payload(to, text, idempotency_key), idempotency_key)

    def send_document(self, to: str, pdf_bytes: bytes, filename: str,
                      caption: str, idempotency_key: str) -> str:
        _validate_pdf(to, pdf_bytes, filename, caption, idempotency_key)
        uploaded = self._json_request("POST", f"{self._base}/{self.phone_number_id}/media",
                                     data={"messaging_product": "whatsapp", "type": "application/pdf"},
                                     files={"file": (filename, pdf_bytes, "application/pdf")})
        media_id = uploaded.get("id")
        if not isinstance(media_id, str) or not re.fullmatch(r"[0-9]{1,40}", media_id):
            raise ProviderError("Meta upload returned no usable media ID; nothing was sent.")
        return self._send({"messaging_product": "whatsapp", "to": to, "type": "document",
                           "document": {"id": media_id, "filename": filename, "caption": caption}}, idempotency_key)


def provider_from_env(env: Mapping[str, str] | None = None) -> WhatsAppProvider:
    """Fail closed for unknown modes or incomplete live approval/configuration."""
    config = os.environ if env is None else env
    mode = config.get("WHATSAPP_PROVIDER", "fake")
    if mode == "fake":
        return FakeWhatsAppProvider()
    if mode != "meta" or config.get("WHATSAPP_PRODUCTION_APPROVED") != "1":
        raise ProviderError("Live WhatsApp transport is disabled pending deployment approval.")
    required = ("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "META_GRAPH_API_VERSION",
                "META_APP_SECRET", "WHATSAPP_VERIFY_TOKEN")
    if any(not config.get(name, "").strip() for name in required):
        raise ProviderError("Live WhatsApp requires complete server-side configuration.")
    return MetaWhatsAppProvider(config[required[0]], config[required[1]], config[required[2]], production_approved=True)
