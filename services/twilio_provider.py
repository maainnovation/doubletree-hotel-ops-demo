"""Twilio WhatsApp transport; hermetic MockTransport tests or explicit live gate.

The durable workflow owns enrollment, 24-hour windows, outbox claims and retries.
Twilio message creation is NOT assumed idempotent. Uncertain sends must be
reconciled, never blindly repeated. Media URLs are capabilities issued by a
trusted private-storage publisher, not caller-provided arbitrary URLs.
"""
from __future__ import annotations

from datetime import timezone
from email.utils import parsedate_to_datetime
import hmac
import ipaddress
import json
import math
import re
from typing import Mapping, Protocol
from urllib.parse import urlsplit

import httpx
from twilio.request_validator import RequestValidator

from services.whatsapp_provider import (
    AmbiguousSendError, ProviderError, RetryableProviderError,
    _media_limits, _recipient_and_key, _validate_pdf,
)

_ACCOUNT = re.compile(r"AC[0-9a-fA-F]{32}")
_MESSAGE = re.compile(r"(?:SM|MM)[0-9a-fA-F]{32}")
_JSON_LIMIT = 64 * 1024


class MediaPublisher(Protocol):
    """Issue a bounded-lifetime URL for this outbox PDF, never a public listing."""
    def publish(self, pdf_bytes: bytes, filename: str, idempotency_key: str) -> str: ...


def _phone_digits(value: str) -> str:
    if isinstance(value, str) and value.startswith("whatsapp:"):
        value = value[len("whatsapp:"):]
    if isinstance(value, str) and value.startswith("+"):
        value = value[1:]
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{6,14}", value):
        raise ProviderError("An explicitly configured international WhatsApp sender is required.")
    return value


def twilio_business_id(account_sid: str, sender: str) -> str:
    if not isinstance(account_sid, str) or not _ACCOUNT.fullmatch(account_sid):
        raise ProviderError("A valid Twilio account SID is required.")
    return f"twilio:{account_sid}:{_phone_digits(sender)}"


def _https_url(value: str) -> bool:
    """Strict externally configured endpoint; no local/IP/userinfo/fragment URLs."""
    try:
        if not isinstance(value, str) or len(value) > 4096 or any(c.isspace() or ord(c) < 32 for c in value):
            return False
        parsed = urlsplit(value)
        host = parsed.hostname
        if (parsed.scheme != "https" or not host or parsed.username is not None
                or parsed.password is not None or parsed.fragment or parsed.port is not None
                or "\\" in value or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host)
                or "." not in host or host.endswith((".localhost", ".local", ".internal"))):
            return False
        try:
            ipaddress.ip_address(host)
            return False
        except ValueError:
            return True
    except ValueError:
        return False


def validate_twilio_signature(auth_token: str, external_url: str,
                              parameters: Mapping[str, str], signature: str) -> bool:
    """Validate documented form HMAC with the EXACT configured external URL.

    All decoded form fields (including unknown fields and whitespace) must be
    supplied. The HTTP boundary rejects duplicate keys before building Mapping.
    JSON/bodySHA256 requests and URL/port normalization are intentionally not
    supported by this form-only endpoint. Vectors match Twilio's official docs.
    """
    if (not isinstance(auth_token, str) or not auth_token or not _https_url(external_url)
            or not isinstance(signature, str) or not re.fullmatch(r"[A-Za-z0-9+/]{27}=", signature)
            or not isinstance(parameters, Mapping)
            or any(not isinstance(k, str) or not isinstance(v, str) for k, v in parameters.items())):
        return False
    # The SDK performs the canonical form signing; compare directly to avoid
    # validate() accepting alternate ports instead of the exact configured URL.
    expected = RequestValidator(auth_token).compute_signature(external_url, parameters)
    return hmac.compare_digest(signature, expected)


def validate_twilio_media_url(url: str, account_sid: str, message_sid: str | None = None) -> bool:
    """Accept only canonical own-account media URLs, optionally bound to message."""
    if not isinstance(account_sid, str) or not _ACCOUNT.fullmatch(account_sid):
        return False
    if message_sid is not None and (not isinstance(message_sid, str) or not _MESSAGE.fullmatch(message_sid)):
        return False
    message = re.escape(message_sid) if message_sid else r"(?:SM|MM)[0-9a-fA-F]{32}"
    expected = (r"https://api\.twilio\.com/2010-04-01/Accounts/" + re.escape(account_sid)
                + r"/Messages/" + message + r"/Media/ME[0-9a-fA-F]{32}")
    return isinstance(url, str) and re.fullmatch(expected, url) is not None


class TwilioWhatsAppProvider:
    """No constructor network. Only exact httpx.MockTransport bypasses live gate.

    Account-SID/Auth-Token Basic auth is supported for a future approved pilot.
    Production credentials, hosting and entitlement remain separate approvals.
    Redirecting inbound media is deliberately rejected (never forward auth to a
    CDN); a live probe may reveal that a reviewed CDN download adapter is needed.
    """
    is_live = True

    def __init__(self, account_sid: str, auth_token: str, sender: str, *,
                 media_publisher: MediaPublisher | None = None,
                 status_callback_url: str | None = None,
                 production_approved: bool = False,
                 transport: httpx.BaseTransport | None = None):
        mocked = type(transport) is httpx.MockTransport
        if not mocked and production_approved is not True:
            raise ProviderError("Live Twilio transport is disabled pending deployment approval.")
        self.phone_number_id = twilio_business_id(account_sid, sender)
        if (not isinstance(auth_token, str) or not auth_token or len(auth_token) > 256
                or any(c.isspace() for c in auth_token)):
            raise ProviderError("A valid server-side Twilio auth token is required.")
        if status_callback_url is not None and not _https_url(status_callback_url):
            raise ProviderError("A configured HTTPS status callback is required.")
        self.is_live = not mocked
        self.account_sid = account_sid
        self.sender = _phone_digits(sender)
        self._auth_token = auth_token
        self._publisher = media_publisher
        self._callback = status_callback_url
        self._transport = transport
        self._base = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}"

    def _client(self) -> httpx.Client:
        return httpx.Client(auth=httpx.BasicAuth(self.account_sid, self._auth_token),
                            timeout=httpx.Timeout(20, connect=5), trust_env=False,
                            follow_redirects=False, transport=self._transport,
                            headers={"Accept-Encoding": "identity"})

    @staticmethod
    def _read(response: httpx.Response, limit: int) -> bytes:
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise ProviderError("Encoded provider responses are not accepted.")
        length = response.headers.get("content-length")
        if length is not None:
            if not re.fullmatch(r"[0-9]+", length) or int(length) > limit:
                raise ProviderError("Provider response length is invalid or exceeds the limit.")
        chunks, total = [], 0
        for chunk in response.iter_bytes(chunk_size=65536):
            total += len(chunk)
            if total > limit:
                raise ProviderError("Provider response exceeds the byte limit.")
            chunks.append(chunk)
        if length is not None and total != int(length):
            raise ProviderError("Provider response length did not match its body.")
        return b"".join(chunks)

    def _json_request(self, method: str, url: str, *, sending: bool = False, **kwargs) -> dict:
        try:
            with self._client() as client, client.stream(method, url, **kwargs) as response:
                if sending and (response.status_code >= 500 or response.status_code == 408):
                    raise AmbiguousSendError("Twilio send outcome is unknown; reconciliation required.")
                if response.status_code == 429 or (not sending and response.status_code >= 500):
                    raise RetryableProviderError("Provider temporarily unavailable before a confirmed send.")
                if not 200 <= response.status_code < 300:
                    raise ProviderError("Provider rejected or redirected the request.")
                try:
                    result = json.loads(self._read(response, _JSON_LIMIT))
                    if not isinstance(result, dict):
                        raise ValueError
                    return result
                except (ValueError, ProviderError):
                    if sending:
                        raise AmbiguousSendError("Twilio acknowledgement unreadable; reconciliation required.") from None
                    raise ProviderError("Invalid provider metadata response.") from None
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise RetryableProviderError("Provider connection failed before sending.") from None
        except httpx.RequestError:
            if sending:
                raise AmbiguousSendError("Twilio send outcome is unknown; reconciliation required.") from None
            raise RetryableProviderError("Provider metadata transfer interrupted.") from None

    def verify_inbound_message(self, message_sid: str, sender: str, now: float) -> float:
        """Read trusted creation time in worker; webhook receipt time is replayable."""
        _recipient_and_key(sender, "inbound-validation")
        if (not isinstance(message_sid, str) or not _MESSAGE.fullmatch(message_sid)
                or isinstance(now, bool) or not isinstance(now, (float, int)) or not math.isfinite(now)):
            raise ProviderError("Invalid inbound message identity or reference time.")
        metadata = self._json_request("GET", f"{self._base}/Messages/{message_sid}.json")
        if (metadata.get("sid") != message_sid or metadata.get("account_sid") != self.account_sid
                or metadata.get("from") != f"whatsapp:+{sender}"
                or metadata.get("to") != f"whatsapp:+{self.sender}"
                or metadata.get("direction") != "inbound"):
            raise ProviderError("Inbound provider metadata does not match the enrolled message.")
        try:
            value = metadata["date_created"]
            if not isinstance(value, str):
                raise ValueError
            created = parsedate_to_datetime(value)
            if created.tzinfo is None:
                raise ValueError
            timestamp = created.astimezone(timezone.utc).timestamp()
            if not math.isfinite(timestamp) or timestamp > now + 300 or now - timestamp >= 24 * 3600:
                raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ProviderError("Inbound provider timestamp is invalid, stale, or too far in the future.") from None
        # Never extend a service window into the future due to clock skew.
        return min(timestamp, float(now))

    def fetch_media(self, media_id: str, expected_mime: str, max_bytes: int) -> bytes:
        _media_limits(expected_mime, max_bytes)
        if not validate_twilio_media_url(media_id, self.account_sid):
            raise ProviderError("Media URL is not a canonical own-account Twilio endpoint.")
        try:
            with self._client() as client, client.stream("GET", media_id) as response:
                if response.status_code == 429 or response.status_code >= 500:
                    raise RetryableProviderError("Media download temporarily unavailable.")
                if not 200 <= response.status_code < 300:
                    raise ProviderError("Media download rejected or redirected.")
                if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != expected_mime:
                    raise ProviderError("Downloaded MIME type does not match the webhook.")
                result = self._read(response, max_bytes)
        except httpx.RequestError:
            raise RetryableProviderError("Media transfer interrupted; safe to retry.") from None
        if not result:
            raise ProviderError("Downloaded media is empty.")
        return result

    def _send(self, to: str, payload: dict) -> str:
        payload = {"From": f"whatsapp:+{self.sender}", "To": f"whatsapp:+{to}", **payload}
        if self._callback:
            payload["StatusCallback"] = self._callback
        # No automatic retry and no invented provider Idempotency-Key guarantee.
        result = self._json_request("POST", f"{self._base}/Messages.json", sending=True, data=payload)
        sid = result.get("sid")
        if (not isinstance(sid, str) or not _MESSAGE.fullmatch(sid)
                or ("account_sid" in result and result["account_sid"] != self.account_sid)
                or ("from" in result and result["from"] != payload["From"])
                or ("to" in result and result["to"] != payload["To"])):
            raise AmbiguousSendError("Twilio returned no matching usable message ID; reconciliation required.")
        return sid

    def send_text(self, to: str, text: str, idempotency_key: str) -> str:
        _recipient_and_key(to, idempotency_key)
        if not isinstance(text, str) or not text.strip() or len(text) > 1600:
            raise ProviderError("Twilio text must contain between 1 and 1600 characters.")
        return self._send(to, {"Body": text})

    def send_document(self, to: str, pdf_bytes: bytes, filename: str,
                      caption: str, idempotency_key: str) -> str:
        _validate_pdf(to, pdf_bytes, filename, caption, idempotency_key)
        if len(filename) > 20:
            raise ProviderError("Twilio PDF filenames must be at most 20 characters.")
        if self._publisher is None:
            raise ProviderError("A private expiring PDF publisher is required; nothing was sent.")
        try:
            url = self._publisher.publish(pdf_bytes, filename, idempotency_key)
        except ValueError:
            raise ProviderError("Private PDF publication was rejected; nothing was sent.") from None
        if not _https_url(url):
            raise ProviderError("Publisher returned an invalid HTTPS media URL; nothing was sent.")
        # Twilio ignores Body for WhatsApp documents. The PDF itself contains
        # the daily calculations/shortages; do not silently promise a caption.
        return self._send(to, {"MediaUrl": url})
