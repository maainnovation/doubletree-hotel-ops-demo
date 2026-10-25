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
from urllib.parse import unquote, urlsplit

import httpx
from twilio.request_validator import RequestValidator

from services.whatsapp_provider import (
    AmbiguousSendError, ProviderError, RetryableProviderError,
    _media_limits, _recipient_and_key, _validate_pdf,
)

_ACCOUNT = re.compile(r"AC[0-9a-fA-F]{32}")
_MESSAGE = re.compile(r"(?:SM|MM)[0-9a-fA-F]{32}")
_JSON_LIMIT = 64 * 1024
_ERROR_CATEGORIES = frozenset({
    'unclassified', 'required_contentsid', 'unsupported_mediaurl', 'trial_restriction',
    'recipient_not_verified', 'sender_not_enabled', 'outside_window', 'media_fetch_or_type_rejected',
})
_ERROR_PARAMETERS = ('Body', 'ContentSid', 'From', 'MediaUrl', 'StatusCallback', 'To')


class ProviderResourceNotFound(ProviderError):
    """Authenticated read returned 404; not an identity/time validation failure."""


def _classify_rejection_message(value):
    """Return fixed diagnostic labels only; this is not proof of root cause.

    Never return, redact-and-return, or retain arbitrary provider text. Unknown,
    negated or conflicting reason phrases remain unclassified. Parameter names
    are vocabulary labels, not parameter values or recommended payload changes.
    """
    if not isinstance(value,str) or len(value)>2048:
        return 'unclassified',()
    normalized=re.sub(r'\s+',' ',value).strip()
    text=normalized.casefold()
    params=[]
    for name in _ERROR_PARAMETERS:
        if name in {'Body','From','To'}:
            present=re.search(r"(?:['\"`]"+name+r"['\"`]|\bparameter\s+"+name+r'\b)',normalized)
        else:
            present=re.search(r'\b'+name+r'\b',normalized)
        if present:
            params.append(name)
    mentions=tuple(params)
    # Do not turn explanatory negations/contradictions into a confident label.
    if (re.search(r'\b(?:not|never)\s+(?:required|missing|unsupported|unverified|outside)\b',text)
            or re.search(r'\b(?:is|are)\s+(?:supported|allowed|verified|enabled)\b',text)
            or re.search(r'\b(?:no|false|if|unless|whether|may|might|example|hypothetical)\b',text)
            or re.search(r"\b(?:isn|aren|wasn|weren|doesn|didn)['’]t\b",text)
            or re.search(r'\bnot (?:true|the (?:case|cause|reason))\b',text)):
        return 'unclassified',mentions
    patterns={
        'required_contentsid':(
            r"\bcontentsid['\"` ]{0,3}(?:parameter )?(?:is required|is missing|must be (?:provided|specified))\b",
            r"\b(?:missing|required) (?:parameter[: ]*)?['\"` ]{0,3}contentsid\b"),
        'unsupported_mediaurl':(
            r"\bmediaurl['\"` ]{0,3}(?:parameter )?is (?:not supported|not allowed|not available|unsupported|prohibited)\b",
            r"\b(?:unsupported|unexpected|unrecognized) (?:parameter[: ]*)?['\"` ]{0,3}mediaurl\b"),
        'trial_restriction':(
            r'\btrial (?:accounts?|users?) (?:do|does) not support\b',
            r'\btrial accounts? cannot send custom messages\b',
            r'\b(?:trial account upgrade is required|upgrade (?:your|the) trial account to)\b'),
        'recipient_not_verified':(
            r'\b(?:recipient|destination|to number) (?:is not verified|is unverified|must be verified)\b',
            r'\bunverified (?:recipient|destination|to number)\b'),
        'sender_not_enabled':(
            r'\b(?:from (?:number|sender|address)|sender) is (?:not enabled|not configured|not found|not authorized|not associated|not registered)\b',
            r"\bcould not find a channel with the specified ['\"`]?from['\"`]? address\b"),
        'outside_window':(
            r'\boutside (?:the )?(?:24[- ]hour (?:customer service )?|customer service |messaging )window\b',
            r'\bcustomer service window has expired\b'),
        'media_fetch_or_type_rejected':(
            r'\b(?:failed|unable) to (?:fetch|download) (?:the )?(?:media|document|pdf)\b',
            r'\bunsupported media type\b',
            r'\b(?:media|document|pdf) (?:has (?:an? )?)?(?:invalid|unsupported) (?:content|mime) type\b'),
    }
    matches={name:[match for rule in rules for match in re.finditer(rule,text)]
             for name,rules in patterns.items()}
    matched=[name for name,spans in matches.items() if spans]
    residual=list(text)
    for spans in matches.values():
        for match in spans:
            residual[match.start():match.end()]=' '*(match.end()-match.start())
    if re.search(r'\b(?:not|never)\b',''.join(residual)):
        return 'unclassified',mentions
    return (matched[0] if len(matched)==1 else 'unclassified'),mentions


class TwilioRequestRejected(ProviderError):
    """Confirmed rejection: bounded numbers/fixed labels only, never raw text."""
    def __init__(self, http_status: int, twilio_error_code: int | None = None, *, provider_message=None):
        if type(http_status) is not int or not 300 <= http_status < 500 or http_status in {408,429}:
            raise ValueError("Not a confirmed Twilio rejection status")
        self.http_status=http_status
        self.twilio_error_code=(twilio_error_code if type(twilio_error_code) is int
                               and 0 <= twilio_error_code <= 999999 else None)
        self.safe_error_code=f"twilio_http_{http_status}"
        if self.twilio_error_code is not None:
            self.safe_error_code+=f"_code_{self.twilio_error_code}"
        category,params=_classify_rejection_message(provider_message)
        # Defense in depth: even an altered classifier cannot persist free text.
        self.provider_error_category=(category if type(category) is str and category in _ERROR_CATEGORIES
                                      else 'unclassified')
        self.parameter_mentions=tuple(name for name in _ERROR_PARAMETERS
                                      if type(params) is tuple and any(type(p) is str and p==name for p in params))
        self.safe_diagnostic_code=self.safe_error_code
        if self.provider_error_category!='unclassified' or self.parameter_mentions:
            self.safe_diagnostic_code+=('_category_'+self.provider_error_category+'_params_'+
                                       ('+'.join(self.parameter_mentions) or 'none'))
        super().__init__(self.safe_error_code)


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


def _twilio_cdn_url(value: str) -> bool:
    """Only the secured Messaging CDN named by Twilio's media-auth guide.

    The provider does not document a stable CDN path/query schema. Treat both
    as opaque capability material obtained ONLY from the authenticated,
    account/message-bound API response. Never accept this URL from a webhook.
    No old public S3 host, subdomains, ports, escapes that alter routing, or
    chained redirects are permitted. No URL or signed query is logged.
    Source: https://help.twilio.com/articles/223183748-Prevent-Unauthorized-Access-to-Your-Media-with-HTTP-Basic-Auth
    """
    if (not isinstance(value, str) or len(value) > 8192 or not value.isascii()
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
            or "\\" in value or re.search(r"%(?![0-9a-fA-F]{2})|%25", value, re.IGNORECASE)):
        return False
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.netloc != "mms.twiliocdn.com"
                or parsed.fragment or not parsed.path.startswith("/")
                or parsed.path.startswith("//")
                or re.search(r"%(?:2f|5c)", parsed.path, re.IGNORECASE)):
            return False
        decoded_path = unquote(parsed.path, errors="strict")
        decoded_query = unquote(parsed.query, errors="strict")
        if (any(segment in {".", ".."} for segment in decoded_path.split("/"))
                or any(ord(c) < 32 or ord(c) == 127 or c == "\\" for c in decoded_path + decoded_query)):
            return False
    except (ValueError, UnicodeError):
        return False
    return True


class TwilioWhatsAppProvider:
    """No constructor network. Only exact httpx.MockTransport bypasses live gate.

    Account-SID/Auth-Token Basic auth is supported for a future approved pilot.
    Production credentials, hosting and entitlement remain separate approvals.
    Direct media responses and one-hop authenticated API redirects to the exact
    documented secured Messaging CDN are supported. The CDN request is made in
    a fresh unauthenticated client; further redirects and public S3 are rejected.
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
                if not sending and response.status_code == 404:
                    raise ProviderResourceNotFound("Provider resource was not found.")
                if not 200 <= response.status_code < 300:
                    if sending and 300 <= response.status_code < 500:
                        # A confirmed rejection is not an uncertain send. Read
                        # bounded numeric code/fixed labels; never retain provider text,
                        # body, headers, URLs or credentials in errors/state/logs.
                        error_code=None
                        provider_message=None
                        try:
                            rejected=json.loads(self._read(response,_JSON_LIMIT))
                            if isinstance(rejected,dict):
                                error_code=rejected.get("code")
                                provider_message=rejected.get("message")
                        except (ValueError,RecursionError,ProviderError,httpx.RequestError):
                            pass
                        raise TwilioRequestRejected(response.status_code,error_code,provider_message=provider_message) from None
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

    def verify_inbound_media_metadata(self, media_url: str, message_sid: str, now: float) -> float:
        """Attest a signed envelope's Media resource, not its sender or arrival time.

        Only the isolated pilot may use this after a Message-resource 404. This
        does not replace normal Message identity/window verification. Media
        creation time alone is NOT a general WhatsApp service-window origin.
        """
        if (not validate_twilio_media_url(media_url, self.account_sid, message_sid)
                or isinstance(now, bool) or not isinstance(now, (float, int)) or not math.isfinite(now)):
            raise ProviderError("Invalid inbound media identity or reference time.")
        metadata = self._json_request("GET", media_url + ".json")
        if (metadata.get("account_sid") != self.account_sid
                or metadata.get("parent_sid") != message_sid
                or metadata.get("sid") != media_url.rsplit("/", 1)[1]
                or metadata.get("content_type") != "application/pdf"):
            raise ProviderError("Inbound media metadata does not match the signed PDF envelope.")
        try:
            value = metadata["date_created"]
            if not isinstance(value, str):
                raise ValueError
            created = parsedate_to_datetime(value)
            if created.tzinfo is None:
                raise ValueError
            timestamp = created.astimezone(timezone.utc).timestamp()
            if not math.isfinite(timestamp) or timestamp > now or now - timestamp >= 24 * 3600:
                raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ProviderError("Inbound media timestamp is invalid, stale, or in the future.") from None
        return timestamp

    def _media_body(self, response: httpx.Response, expected_mime: str, max_bytes: int) -> bytes:
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableProviderError("Media download temporarily unavailable.")
        if not 200 <= response.status_code < 300:
            raise ProviderError("Media download rejected or redirected.")
        if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != expected_mime:
            raise ProviderError("Downloaded MIME type does not match the webhook.")
        result = self._read(response, max_bytes)
        if not result:
            raise ProviderError("Downloaded media is empty.")
        return result

    def fetch_media(self, media_id: str, expected_mime: str, max_bytes: int) -> bytes:
        _media_limits(expected_mime, max_bytes)
        if not validate_twilio_media_url(media_id, self.account_sid):
            raise ProviderError("Media URL is not a canonical own-account Twilio endpoint.")
        try:
            with self._client() as client, client.stream("GET", media_id) as response:
                if response.status_code not in {301, 302, 303, 307, 308}:
                    return self._media_body(response, expected_mime, max_bytes)
                locations = response.headers.get_list("location")
                if len(locations) != 1 or not _twilio_cdn_url(locations[0]):
                    raise ProviderError("Media download redirected outside the approved secured CDN.")
                location = locations[0]
            # NEVER reuse the authenticated API client, cookie jar, request or
            # headers. A signed CDN capability needs no account credentials.
            with httpx.Client(auth=None, timeout=httpx.Timeout(20, connect=5),
                              trust_env=False, follow_redirects=False,
                              transport=self._transport,
                              headers={"Accept-Encoding": "identity"}) as client:
                with client.stream("GET", location) as response:
                    return self._media_body(response, expected_mime, max_bytes)
        except httpx.RequestError:
            raise RetryableProviderError("Media transfer interrupted; safe to retry.") from None

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
