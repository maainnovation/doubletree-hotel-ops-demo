"""Twilio API transport tests are hermetic: httpx.MockTransport only."""
import base64
from datetime import datetime, timezone
from email.utils import format_datetime
from urllib.parse import parse_qs

import httpx
import pytest
from twilio.request_validator import RequestValidator

from services.twilio_provider import (
    TwilioWhatsAppProvider, twilio_business_id,
    validate_twilio_media_url, validate_twilio_signature,
)
from services.whatsapp_provider import AmbiguousSendError, ProviderError, RetryableProviderError


ACCOUNT = "AC" + "a" * 32
MESSAGE = "SM" + "b" * 32
MEDIA = "ME" + "c" * 32
OUTBOUND = "SM" + "d" * 32
SENDER = "15555550100"
MANAGER = "15555550123"
TOKEN = "synthetic-test-token"
URL = f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT}/Messages/{MESSAGE}/Media/{MEDIA}"
MEDIA_URL = "https://pilot.example.test/whatsapp/media/synthetic-capability/roster.pdf"
CALLBACK_URL = "https://pilot.example.test/webhooks/twilio/status"
PDF = b"%PDF-1.4\nsynthetic-only\n%%EOF"
NOW = datetime(2026, 10, 25, 12, tzinfo=timezone.utc).timestamp()


class Publisher:
    def __init__(self, url=MEDIA_URL):
        self.url, self.calls = url, []

    def publish(self, pdf_bytes, filename, idempotency_key):
        self.calls.append((pdf_bytes, filename, idempotency_key))
        return self.url


def provider(handler, **kwargs):
    return TwilioWhatsAppProvider(ACCOUNT, TOKEN, SENDER,
                                 transport=httpx.MockTransport(handler), **kwargs)


def metadata(**overrides):
    return {"sid": MESSAGE, "account_sid": ACCOUNT, "from": f"whatsapp:+{MANAGER}",
            "to": f"whatsapp:+{SENDER}", "direction": "inbound",
            "date_created": format_datetime(datetime.fromtimestamp(NOW - 60, timezone.utc)), **overrides}


def test_official_twilio_documented_signature_vector():
    # https://www.twilio.com/docs/usage/security (official worked example).
    params = {"Digits": "1234", "To": "+18005551212", "From": "+14158675310",
              "Caller": "+14158675310", "CallSid": "CA1234567890ABCDE"}
    assert validate_twilio_signature("12345", "https://example.com/myapp.php?foo=1&bar=2",
                                     params, "L/OH5YylLD5NRKLltdqwSvS0BnU=")


def test_official_sdk_signature_vector_and_exact_url_binding():
    params = {"CallSid": "CA1234567890ABCDE", "Digits": "1234", "From": "+14158675309",
              "To": "+18005551212", "Caller": "+14158675309"}
    url = "https://mycompany.com/myapp.php?foo=1&bar=2"
    signature = "RSOYDt4T1cUTdK1PDd93/VVr8B8="
    assert validate_twilio_signature("12345", url, params, signature)
    for alternate in [url + "/", url.replace("foo=1&bar=2", "bar=2&foo=1"),
                      url.replace(".com", ".com:443"), url.replace("https:", "http:")]:
        assert not validate_twilio_signature("12345", alternate, params, signature)
    assert not validate_twilio_signature("different", url, params, signature)


def test_signature_covers_unknown_fields_whitespace_and_unicode():
    params = {"Body": "  hello + café  ", "NewTwilioField": "arbitrary value", "From": f"whatsapp:+{MANAGER}"}
    signature = RequestValidator(TOKEN).compute_signature(CALLBACK_URL, params)
    assert validate_twilio_signature(TOKEN, CALLBACK_URL, params, signature)
    assert not validate_twilio_signature(TOKEN, CALLBACK_URL, {**params, "Body": params["Body"].strip()}, signature)
    assert not validate_twilio_signature(TOKEN, CALLBACK_URL, {"Body": params["Body"], "From": params["From"]}, signature)


@pytest.mark.parametrize("signature", [None, "", "not base64", "A" * 27 + "=", "☃" * 28, "A" * 200])
def test_bad_signatures_fail_closed(signature):
    assert not validate_twilio_signature(TOKEN, CALLBACK_URL, {"Body": "test"}, signature)


@pytest.mark.parametrize("params", [{"Body": None}, {1: "test"}, {"Body": ["x", "y"]}, None])
def test_invalid_form_mappings_fail_closed(params):
    assert not validate_twilio_signature(TOKEN, CALLBACK_URL, params, "A" * 27 + "=")


@pytest.mark.parametrize("sender", [SENDER, "+" + SENDER, "whatsapp:+" + SENDER])
def test_business_identity_is_account_and_sender_scoped(sender):
    assert twilio_business_id(ACCOUNT, sender) == f"twilio:{ACCOUNT}:{SENDER}"
    assert twilio_business_id("AC" + "1" * 32, sender) != twilio_business_id(ACCOUNT, sender)


def test_live_constructor_is_gated_and_mock_is_not_live():
    with pytest.raises(ProviderError):
        TwilioWhatsAppProvider(ACCOUNT, TOKEN, SENDER)
    assert provider(lambda _: httpx.Response(200)).is_live is False
    class OtherTransport(httpx.BaseTransport):
        pass
    with pytest.raises(ProviderError):
        TwilioWhatsAppProvider(ACCOUNT, TOKEN, SENDER, transport=OtherTransport())
    class MockSubclass(httpx.MockTransport):
        pass
    with pytest.raises(ProviderError):
        TwilioWhatsAppProvider(ACCOUNT, TOKEN, SENDER, transport=MockSubclass(lambda _: httpx.Response(200)))


def test_approved_construction_has_no_network(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("constructor must not create an HTTP client")
    monkeypatch.setattr(httpx, "Client", no_network)
    item = TwilioWhatsAppProvider(ACCOUNT, TOKEN, SENDER, production_approved=True)
    assert item.is_live is True
    assert item.phone_number_id == f"twilio:{ACCOUNT}:{SENDER}"


@pytest.mark.parametrize("account,token,sender", [
    ("ACbad", TOKEN, SENDER), (ACCOUNT, "", SENDER), (ACCOUNT, "has spaces", SENDER),
    (ACCOUNT, TOKEN, "+01234"), (ACCOUNT, TOKEN, "whatsapp:1555"),
])
def test_invalid_configuration(account, token, sender):
    with pytest.raises(ProviderError):
        TwilioWhatsAppProvider(account, token, sender, transport=httpx.MockTransport(lambda _: httpx.Response(200)))


def test_fetch_uses_account_auth_and_exact_media_endpoint():
    seen = []
    def handler(request):
        seen.append(request)
        assert request.method == "GET" and str(request.url) == URL
        assert request.headers["authorization"] == "Basic " + base64.b64encode(f"{ACCOUNT}:{TOKEN}".encode()).decode()
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, content=PDF, headers={"Content-Type": "application/pdf"})
    assert provider(handler).fetch_media(URL, "application/pdf", 1024) == PDF
    assert len(seen) == 1
    assert validate_twilio_media_url(URL, ACCOUNT, MESSAGE)
    assert not validate_twilio_media_url(URL, ACCOUNT, "SM" + "f" * 32)


@pytest.mark.parametrize("url", [
    URL.replace("https:", "http:"), URL.replace("api.twilio.com", "api.twilio.com.evil.test"),
    URL.replace("api.twilio.com", "127.0.0.1"), URL.replace("api.twilio.com", "api.twilio.com:443"),
    URL.replace("api.twilio.com", "api.twilio.com@evil.test"), URL.replace("api.twilio.com", "user:pass@api.twilio.com"),
    URL.replace(ACCOUNT, "AC" + "f" * 32), URL + ".json", URL + "?token=secret", URL + "#fragment",
    URL.replace("/Media/", "/../Media/"), URL.replace("/Media/", "/%4dedia/"), URL + "/", "file:///etc/passwd",
])
def test_untrusted_media_never_receives_credentials(url):
    seen = []
    def handler(request):
        seen.append(request)
        raise AssertionError("invalid URL must not be fetched")
    assert not validate_twilio_media_url(url, ACCOUNT)
    with pytest.raises(ProviderError):
        provider(handler).fetch_media(url, "application/pdf", 1024)
    assert not seen


def test_media_redirect_never_followed():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://cdn.evil.test/file.pdf"})
    with pytest.raises(ProviderError, match="redirected"):
        provider(handler).fetch_media(URL, "application/pdf", 1024)
    assert len(seen) == 1


@pytest.mark.parametrize("headers,body", [
    ({"Content-Type": "text/html"}, PDF), ({"Content-Type": "application/pdf"}, b""),
    ({"Content-Type": "application/pdf"}, b"x" * 2048),
    ({"Content-Type": "application/pdf", "Content-Length": "99999"}, PDF),
    ({"Content-Type": "application/pdf", "Content-Length": "wrong"}, PDF),
    ({"Content-Type": "application/pdf", "Content-Length": "1"}, PDF),
    ({"Content-Type": "application/pdf", "Content-Encoding": "br"}, PDF),
])
def test_media_type_size_length_and_encoding_are_checked(headers, body):
    with pytest.raises((ProviderError, RetryableProviderError)):
        provider(lambda _: httpx.Response(200, headers=headers, content=body)).fetch_media(URL, "application/pdf", 1024)


def test_verify_inbound_reads_trusted_metadata_before_service_window():
    def handler(request):
        assert str(request.url) == f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT}/Messages/{MESSAGE}.json"
        return httpx.Response(200, json=metadata())
    assert provider(handler).verify_inbound_message(MESSAGE, MANAGER, NOW) == NOW - 60


@pytest.mark.parametrize("override", [
    {"sid": "SM" + "f" * 32}, {"account_sid": "AC" + "f" * 32},
    {"from": "whatsapp:+15555550999"}, {"to": "whatsapp:+15555550999"},
    {"direction": "outbound-api"}, {"date_created": None}, {"date_created": "bad"},
    {"date_created": "Sun, 25 Oct 2026 11:59:00"},
    {"date_created": format_datetime(datetime.fromtimestamp(NOW - 86400, timezone.utc))},
    {"date_created": format_datetime(datetime.fromtimestamp(NOW + 301, timezone.utc))},
])
def test_inbound_metadata_identity_and_freshness_fail_closed(override):
    with pytest.raises(ProviderError):
        provider(lambda _: httpx.Response(200, json=metadata(**override))).verify_inbound_message(MESSAGE, MANAGER, NOW)


def test_small_positive_clock_skew_does_not_extend_window():
    result = metadata(date_created=format_datetime(datetime.fromtimestamp(NOW + 30, timezone.utc)))
    assert provider(lambda _: httpx.Response(200, json=result)).verify_inbound_message(MESSAGE, MANAGER, NOW) == NOW


@pytest.mark.parametrize("now", [True, float("nan"), float("inf"), "today"])
def test_reference_time_must_be_finite(now):
    with pytest.raises(ProviderError):
        provider(lambda _: (_ for _ in ()).throw(AssertionError("must not fetch"))).verify_inbound_message(MESSAGE, MANAGER, now)


def test_pdf_payload_uses_one_media_url_not_ignored_body():
    seen, publisher = [], Publisher()
    def handler(request):
        seen.append(request)
        assert request.method == "POST"
        assert str(request.url) == f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT}/Messages.json"
        assert parse_qs(request.content.decode()) == {
            "From": [f"whatsapp:+{SENDER}"], "To": [f"whatsapp:+{MANAGER}"],
            "MediaUrl": [MEDIA_URL], "StatusCallback": [CALLBACK_URL],
        }
        assert "idempotency-key" not in request.headers
        return httpx.Response(201, json={"sid": OUTBOUND, "account_sid": ACCOUNT})
    item = provider(handler, media_publisher=publisher, status_callback_url=CALLBACK_URL)
    assert item.send_document(MANAGER, PDF, "roster-20261025.pdf", "Needs attention", "outbox:1") == OUTBOUND
    assert publisher.calls == [(PDF, "roster-20261025.pdf", "outbox:1")]
    assert len(seen) == 1


def test_text_payload_uses_body():
    def handler(request):
        assert parse_qs(request.content.decode()) == {"From": [f"whatsapp:+{SENDER}"],
            "To": [f"whatsapp:+{MANAGER}"], "Body": ["Please resend a readable PDF."]}
        return httpx.Response(201, json={"sid": OUTBOUND})
    assert provider(handler).send_text(MANAGER, "Please resend a readable PDF.", "outbox:2") == OUTBOUND


@pytest.mark.parametrize("url", ["http://pilot.example.test/pdf", "https://127.0.0.1/pdf", "https://localhost/pdf",
    "https://server.internal/pdf", "https://user:secret@pilot.example.test/pdf", "https://pilot.example.test:444/pdf",
    "https://pilot.example.test/pdf#fragment", "file:///tmp/roster.pdf", None])
def test_published_url_is_checked_before_send(url):
    seen = []
    item = provider(lambda request: seen.append(request), media_publisher=Publisher(url))
    with pytest.raises(ProviderError):
        item.send_document(MANAGER, PDF, "roster.pdf", "", "outbox:3")
    assert not seen


def test_document_requires_publisher_and_short_safe_filename():
    def never(request):
        raise AssertionError("must not send")
    with pytest.raises(ProviderError):
        provider(never).send_document(MANAGER, PDF, "roster.pdf", "", "outbox:4")
    with pytest.raises(ProviderError):
        provider(never, media_publisher=Publisher()).send_document(MANAGER, PDF, "roster-" + "x" * 30 + ".pdf", "", "outbox:4")


@pytest.mark.parametrize("status", [500, 503, 408])
def test_unknown_send_http_outcomes_require_reconciliation(status):
    with pytest.raises(AmbiguousSendError):
        provider(lambda _: httpx.Response(status, json={"secret": TOKEN})).send_text(MANAGER, "test", "outbox:5")


@pytest.mark.parametrize("body", [{}, [], {"sid": "not-a-SID"}, {"sid": OUTBOUND, "account_sid": "wrong"},
    {"sid": OUTBOUND, "to": "whatsapp:+15555550999"}, {"sid": OUTBOUND, "from": "wrong"}])
def test_bad_send_acknowledgement_requires_reconciliation(body):
    with pytest.raises(AmbiguousSendError):
        provider(lambda _: httpx.Response(201, json=body)).send_text(MANAGER, "test", "outbox:6")


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.WriteError, httpx.ReadError])
def test_uncertain_network_send_never_retried_or_leaks_secrets(error):
    seen = []
    def handler(request):
        seen.append(request)
        raise error("secret " + TOKEN, request=request)
    with pytest.raises(AmbiguousSendError) as exc:
        provider(handler).send_text(MANAGER, "test", "outbox:7")
    assert TOKEN not in str(exc.value)
    assert len(seen) == 1


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout])
def test_connection_failure_is_safe_to_retry(error):
    def handler(request):
        raise error("secret " + TOKEN, request=request)
    with pytest.raises(RetryableProviderError) as exc:
        provider(handler).send_text(MANAGER, "test", "outbox:8")
    assert TOKEN not in str(exc.value)


def test_rate_limit_safe_to_retry_and_rejection_not_retried():
    with pytest.raises(RetryableProviderError):
        provider(lambda _: httpx.Response(429)).send_text(MANAGER, "test", "outbox:9")
    with pytest.raises(ProviderError) as exc:
        provider(lambda _: httpx.Response(401, text=TOKEN)).send_text(MANAGER, "test", "outbox:10")
    assert not isinstance(exc.value, RetryableProviderError)
    assert TOKEN not in str(exc.value)


def test_publisher_rejection_is_sanitized_and_never_sends():
    class RejectingPublisher:
        def publish(self, *args):
            raise ValueError("private token "+TOKEN)
    seen = []
    item = provider(lambda request: seen.append(request), media_publisher=RejectingPublisher())
    with pytest.raises(ProviderError) as exc:
        item.send_document(MANAGER, PDF, "roster.pdf", "", "outbox:11")
    assert TOKEN not in str(exc.value)
    assert not seen


# The path/query are fictional opaque capability material, not a claim about
# Twilio's undocumented path schema. Only its documented CDN hostname is fixed.
CDN_URL = "https://mms.twiliocdn.com/synthetic/object.pdf?Expires=123&Signature=opaque%2Btoken&Key-Pair-Id=synthetic"


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_documented_secured_cdn_single_hop_never_receives_auth_cookies_or_referrer(status):
    seen = []
    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            assert str(request.url) == URL
            assert request.headers.get("authorization", "").startswith("Basic ")
            return httpx.Response(status, headers={"Location": CDN_URL,
                "Set-Cookie": "api-session=secret; Domain=.twiliocdn.com; Path=/"})
        assert str(request.url) == CDN_URL
        assert request.url.query == b"Expires=123&Signature=opaque%2Btoken&Key-Pair-Id=synthetic"
        for header in ("authorization", "cookie", "proxy-authorization", "referer"):
            assert header not in request.headers
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, content=PDF, headers={"Content-Type": "application/pdf"})
    assert provider(handler).fetch_media(URL, "application/pdf", 1024) == PDF
    assert len(seen) == 2


@pytest.mark.parametrize("location", [
    "https://s3-external-1.amazonaws.com/bucket/object.pdf?token=synthetic",
    "https://bucket.s3.amazonaws.com/object.pdf?token=synthetic",
    "https://mms.twiliocdn.com.evil.test/object.pdf?token=synthetic",
    "https://sub.mms.twiliocdn.com/object.pdf?token=synthetic",
    "http://mms.twiliocdn.com/object.pdf?token=synthetic",
    "https://127.0.0.1/object.pdf?token=synthetic",
    "https://[::1]/object.pdf?token=synthetic",
    "https://localhost/object.pdf?token=synthetic",
    "https://mms.twiliocdn.com:443/object.pdf?token=synthetic",
    "https://user:password@mms.twiliocdn.com/object.pdf?token=synthetic",
    "https://mms.twiliocdn.com@evil.test/object.pdf?token=synthetic",
    "https://mms.twiliocdn.com/object.pdf?token=synthetic#fragment",
    "https://mms.twiliocdn.com//evil.test/object.pdf",
    "https://mms.twiliocdn.com/../object.pdf",
    "https://mms.twiliocdn.com/./object.pdf",
    "https://mms.twiliocdn.com/%2e%2e/object.pdf",
    "https://mms.twiliocdn.com/%252e%252e/object.pdf",
    "https://mms.twiliocdn.com/path%2f..%2fobject.pdf",
    "https://mms.twiliocdn.com/%5cobject.pdf",
    "https://mms.twiliocdn.com/object.pdf?token=%250a",
    "https://mms.twiliocdn.com/object.pdf?token=%0a",
    "https://mms.twiliocdn.com/object.pdf?token=%ff",
    "https://mms.twiliocdn.com/object.pdf?token=%XX",
    "https://mms.twiliocdn.com/object.pdf?token=\nsecret",
    "https://mms.twiliocdn.com/object.pdf?token=secret\x7f",
    "/relative/object.pdf", "//mms.twiliocdn.com/object.pdf", "",
    "https://mms.twiliocdn.com/object.pdf?token=" + "x" * 8200,
])
def test_unapproved_or_ambiguous_cdn_locations_fail_before_second_request(location):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": location})
    with pytest.raises(ProviderError):
        provider(handler).fetch_media(URL, "application/pdf", 1024)
    assert len(seen) == 1


def test_cdn_location_is_not_accepted_as_initial_media_url():
    seen = []
    with pytest.raises(ProviderError):
        provider(lambda request: seen.append(request)).fetch_media(CDN_URL, "application/pdf", 1024)
    assert not seen


def test_multiple_location_headers_are_ambiguous():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers=[("Location", CDN_URL), ("Location", CDN_URL)])
    with pytest.raises(ProviderError):
        provider(handler).fetch_media(URL, "application/pdf", 1024)
    assert len(seen) == 1


@pytest.mark.parametrize("second_location", [CDN_URL, "https://api.twilio.com/private", "https://evil.test/file"])
def test_cdn_redirect_chains_are_never_followed(second_location):
    seen = []
    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(302, headers={"Location": CDN_URL})
        assert "authorization" not in request.headers
        return httpx.Response(302, headers={"Location": second_location})
    with pytest.raises(ProviderError):
        provider(handler).fetch_media(URL, "application/pdf", 1024)
    assert len(seen) == 2


@pytest.mark.parametrize("headers,body", [
    ({"Content-Type": "text/html"}, PDF),
    ({"Content-Type": "application/pdf"}, b""),
    ({"Content-Type": "application/pdf"}, b"x" * 2048),
    ({"Content-Type": "application/pdf", "Content-Length": "999999"}, PDF),
    ({"Content-Type": "application/pdf", "Content-Encoding": "br"}, PDF),
])
def test_cdn_content_has_same_type_and_bounded_size_policy(headers, body):
    seen = []
    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(302, headers={"Location": CDN_URL})
        return httpx.Response(200, headers=headers, content=body)
    with pytest.raises(ProviderError):
        provider(handler).fetch_media(URL, "application/pdf", 1024)
    assert len(seen) == 2


def test_cdn_transfer_errors_do_not_leak_signed_capability():
    seen = []
    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(302, headers={"Location": CDN_URL})
        raise httpx.ReadTimeout("secret capability " + CDN_URL, request=request)
    with pytest.raises(RetryableProviderError) as exc:
        provider(handler).fetch_media(URL, "application/pdf", 1024)
    assert "opaque" not in str(exc.value) and CDN_URL not in str(exc.value)
    assert len(seen) == 2
