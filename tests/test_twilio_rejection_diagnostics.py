"""Safe Twilio rejection diagnostics: mocked only, never retain response prose."""
from email.utils import format_datetime
import gzip
import json
import traceback

import httpx
import pytest

from scripts.demo_whatsapp_workflow import DEMO_CODE, DEMO_NOW, DEMO_SENDER, forecast_pdf
from services.staff_settings import demo_defaults
from services.twilio_provider import TwilioWhatsAppProvider, twilio_business_id
from services.whatsapp_provider import AmbiguousSendError, ProviderError, RetryableProviderError
from services.whatsapp_store import PrivateFileStore, WorkflowStore
from services.whatsapp_workflow import ForecastWorker


ACCOUNT, MESSAGE = "AC" + "a" * 32, "SM" + "b" * 32
BUSINESS, TOKEN = "15555550900", "synthetic-diagnostic-auth-token"
SENSITIVE = "PRIVATE_RESPONSE_PHONE_15555559876_SECRET_DO_NOT_PERSIST"
CAPABILITY = "https://synthetic.example.test/media/twilio/secret-capability/roster.pdf"
MEDIA_URL = f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT}/Messages/{MESSAGE}/Media/ME" + "c" * 32


def provider(handler):
    return TwilioWhatsAppProvider(ACCOUNT, TOKEN, BUSINESS, transport=httpx.MockTransport(handler))


def rejected(response):
    from services.twilio_provider import TwilioRequestRejected
    with pytest.raises(TwilioRequestRejected) as error:
        provider(lambda request: response).send_text(DEMO_SENDER, "Synthetic test", "diagnostic-test")
    return error.value


def assert_safe(error):
    representations = str(error) + repr(error) + repr(vars(error)) + "".join(traceback.format_exception(error))
    for value in (SENSITIVE, TOKEN, CAPABILITY):
        assert value not in representations
    for name in ("body", "response", "request", "headers", "url", "message", "more_info"):
        assert not hasattr(error, name)


@pytest.mark.parametrize("status", [300, 301, 302, 307, 308, 400, 401, 403, 404, 422, 499])
def test_explicit_rejections_expose_only_validated_status_and_numeric_code(status):
    error = rejected(httpx.Response(status, json={"code": 21608, "message": SENSITIVE,
        "more_info": CAPABILITY, "auth_token": TOKEN}, headers={"Location": CAPABILITY}))
    assert error.http_status == status and error.twilio_error_code == 21608
    assert error.safe_error_code == f"twilio_http_{status}_code_21608"
    assert isinstance(error, ProviderError) and not isinstance(error, (RetryableProviderError, AmbiguousSendError))
    assert_safe(error)


@pytest.mark.parametrize("code", [0, 1, 21608, 999999])
def test_numeric_error_code_inclusive_bounds(code):
    error = rejected(httpx.Response(403, json={"code": code, "message": SENSITIVE}))
    assert error.twilio_error_code == code and error.safe_error_code == f"twilio_http_403_code_{code}"


@pytest.mark.parametrize("code", [True, False, "21608", SENSITIVE, -1, 1000000, 21608.0, None, [], {}, float("nan"), float("inf")])
def test_untrusted_or_out_of_range_error_codes_are_not_serialized(code):
    raw = json.dumps({"code": code, "message": SENSITIVE, "url": CAPABILITY}).encode()
    error = rejected(httpx.Response(403, content=raw))
    assert error.http_status == 403 and error.twilio_error_code is None
    assert error.safe_error_code == "twilio_http_403"
    assert_safe(error)


@pytest.mark.parametrize("raw", [b"", b"not-json", b"[]", b"null", b"true", b'"text"', b'{"code":21608,',
    json.dumps({"message": SENSITIVE}).encode(), b'{"code":' + b"9" * 5000 + b"}",
    b'{"code":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}",
    json.dumps({"code": 21608, "message": SENSITIVE + "x" * 65537}).encode()])
def test_malformed_missing_truncated_or_oversize_json_keeps_status_only(raw):
    error = rejected(httpx.Response(403, content=raw))
    assert error.safe_error_code == "twilio_http_403" and error.twilio_error_code is None
    assert_safe(error)


@pytest.mark.parametrize("headers", [{"Content-Length": "70000"}, {"Content-Length": "-1"},
    {"Content-Length": "invalid"}, {"Content-Length": "1"}, {"Content-Encoding": "gzip"}])
def test_invalid_length_or_encoded_error_body_does_not_capture_code(headers):
    raw = json.dumps({"code": 21608, "message": SENSITIVE}).encode()
    if headers.get("Content-Encoding") == "gzip":
        raw = gzip.compress(raw)
    error = rejected(httpx.Response(403, content=raw, headers=headers))
    assert error.safe_error_code == "twilio_http_403"
    assert_safe(error)


def test_diagnostic_stream_interruption_after_known_rejection_is_status_only():
    class Truncated(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"code":21608,"message":"'
            raise httpx.ReadError(SENSITIVE)
    error = rejected(httpx.Response(403, stream=Truncated()))
    assert error.safe_error_code == "twilio_http_403"
    assert_safe(error)


@pytest.mark.parametrize("status,kind", [(429, RetryableProviderError), (408, AmbiguousSendError),
    (500, AmbiguousSendError), (503, AmbiguousSendError)])
def test_retry_and_ambiguous_http_semantics_are_unchanged(status, kind):
    from services.twilio_provider import TwilioRequestRejected
    with pytest.raises(kind) as error:
        provider(lambda request: httpx.Response(status, json={"code": 21608, "message": SENSITIVE})).send_text(DEMO_SENDER, "Synthetic", "key")
    assert not isinstance(error.value, TwilioRequestRejected)
    assert SENSITIVE not in str(error.value)


@pytest.mark.parametrize("failure,kind", [(httpx.ReadTimeout, AmbiguousSendError),
    (httpx.ReadError, AmbiguousSendError), (httpx.ConnectError, RetryableProviderError)])
def test_network_uncertainty_is_not_misreported_as_confirmed_rejection(failure, kind):
    from services.twilio_provider import TwilioRequestRejected
    def transport(request):
        raise failure(SENSITIVE, request=request)
    with pytest.raises(kind) as error:
        provider(transport).send_text(DEMO_SENDER, "Synthetic", "key")
    assert not isinstance(error.value, TwilioRequestRejected)
    assert SENSITIVE not in str(error.value)


def test_unreadable_success_acknowledgement_remains_ambiguous():
    from services.twilio_provider import TwilioRequestRejected
    with pytest.raises(AmbiguousSendError) as error:
        provider(lambda request: httpx.Response(201, content=SENSITIVE.encode())).send_text(DEMO_SENDER, "Synthetic", "key")
    assert not isinstance(error.value, TwilioRequestRejected)


def test_non_send_resource_404_remains_distinct_and_does_not_parse_sensitive_body():
    from services.twilio_provider import ProviderResourceNotFound, TwilioRequestRejected
    with pytest.raises(ProviderResourceNotFound) as error:
        provider(lambda request: httpx.Response(404, json={"code": 20404, "message": SENSITIVE})).verify_inbound_message(MESSAGE, DEMO_SENDER, DEMO_NOW.timestamp())
    assert not isinstance(error.value, TwilioRequestRejected)
    assert SENSITIVE not in str(error.value)


@pytest.mark.parametrize("diagnostic,expected", [({"code": 21608}, "twilio_http_403_code_21608"),
    ({"code": SENSITIVE}, "twilio_http_403"), ({}, "twilio_http_403")])
def test_pdf_outbox_persists_safe_code_once_without_response_or_auto_retry(tmp_path, diagnostic, expected):
    store = WorkflowStore(tmp_path / "state.sqlite3")
    files = PrivateFileStore(tmp_path / "private")
    phone = twilio_business_id(ACCOUNT, BUSINESS)
    store.enroll_tenant(tenant_id="synthetic", manager_id="gm", hotel_code=DEMO_CODE, hotel_name="Fictional Diagnostic Hotel")
    store.enroll_sender(tenant_id="synthetic", phone_number_id=phone, sender=DEMO_SENDER)
    store.save_staff_settings("gm", demo_defaults(), dataset_mode="demo")
    store.save_hotel_parameters("gm", {"room_capacity": 400})
    source, sends, clock = forecast_pdf(), [], [DEMO_NOW.timestamp()]
    class Publisher:
        def publish(self, *_):
            return CAPABILITY
    def transport(request):
        assert request.url.host == "api.twilio.com"
        if request.method == "GET" and request.url.path.endswith(".json"):
            return httpx.Response(200, json={"sid": MESSAGE, "account_sid": ACCOUNT,
                "from": "whatsapp:+" + DEMO_SENDER, "to": "whatsapp:+" + BUSINESS,
                "direction": "inbound", "date_created": format_datetime(DEMO_NOW)})
        if request.method == "GET":
            return httpx.Response(200, content=source, headers={"Content-Type": "application/pdf"})
        sends.append(request.method)
        return httpx.Response(403, json={**diagnostic, "message": SENSITIVE, "more_info": CAPABILITY, "token": TOKEN})
    transport_provider = TwilioWhatsAppProvider(ACCOUNT, TOKEN, BUSINESS, media_publisher=Publisher(), transport=httpx.MockTransport(transport))
    worker = ForecastWorker(store, files, transport_provider, clock=lambda: clock[0])
    store.ingest_twilio({"AccountSid": ACCOUNT, "MessageSid": MESSAGE, "From": "whatsapp:+" + DEMO_SENDER,
        "To": "whatsapp:+" + BUSINESS, "NumMedia": "1", "MediaContentType0": "application/pdf", "MediaUrl0": MEDIA_URL},
        clock[0], account_sid=ACCOUNT, business_sender=BUSINESS)
    assert worker.drain_ready() == {"processed": 1, "dispatched": 1}
    outbox = store.rows("wa_outbox")[0]
    assert outbox["status"] == "failed" and outbox["error_code"] == expected and outbox["attempts"] == 1
    assert outbox["provider_message_id"] is None and outbox["kind"] == "document"
    assert len(store.list_weekly_roster_versions("gm")) == 1
    clock[0] += 3600
    assert worker.drain_ready() == {"processed": 0, "dispatched": 0} and len(sends) == 1
    with store._connect() as db:
        dump = "\n".join(db.iterdump())
    assert all(value not in dump for value in (SENSITIVE, TOKEN, CAPABILITY))
