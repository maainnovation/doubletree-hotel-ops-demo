"""Bounded, lossy rejection classification; all provider traffic is mocked."""
from email.utils import format_datetime
import re
import traceback

import httpx
import pytest

from scripts.demo_whatsapp_workflow import DEMO_CODE, DEMO_NOW, DEMO_SENDER, forecast_pdf
from services.staff_settings import demo_defaults
from services.twilio_provider import TwilioRequestRejected, TwilioWhatsAppProvider, twilio_business_id
from services.whatsapp_provider import AmbiguousSendError, RetryableProviderError
from services.whatsapp_store import PrivateFileStore, WorkflowStore
from services.whatsapp_workflow import ForecastWorker


ACCOUNT, MESSAGE = "AC" + "a" * 32, "SM" + "b" * 32
BUSINESS, TOKEN = "15555550900", "synthetic-classification-auth-token"
CAPABILITY = "https://synthetic.test/media/twilio/PRIVATE_CAPABILITY/roster.pdf"
MEDIA_URL = f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT}/Messages/{MESSAGE}/Media/ME" + "c" * 32
PRIVATE_PARTS = ("PRIVATE_PATIENT_NAME", "+15555559876", "AC" + "f" * 32,
                 "PRIVATE_ADDRESS_17_FICTIONAL_STREET", "private-person@synthetic.test", TOKEN, CAPABILITY)
PRIVATE = " | ".join(PRIVATE_PARTS)
ALLOWED_PARAMS = {"MediaUrl", "ContentSid", "StatusCallback", "From", "To", "Body"}
ALLOWED_CATEGORIES = {"required_contentsid", "unsupported_mediaurl", "trial_restriction",
    "recipient_not_verified", "sender_not_enabled", "outside_window", "media_fetch_or_type_rejected", "unclassified"}


def classify(value):
    from services.twilio_provider import _classify_rejection_message
    category, params = _classify_rejection_message(value)
    assert category in ALLOWED_CATEGORIES
    assert isinstance(params, tuple) and len(set(params)) == len(params)
    assert set(params) <= ALLOWED_PARAMS
    return category, params


@pytest.mark.parametrize("message,expected", [
    ("ContentSid is required.", "required_contentsid"),
    ("The ContentSid parameter is required.", "required_contentsid"),
    ("MediaUrl is not supported.", "unsupported_mediaurl"),
    ("The MediaUrl parameter is unsupported.", "unsupported_mediaurl"),
    ("Trial accounts cannot send custom messages.", "trial_restriction"),
    ("Trial accounts do not support custom messages.", "trial_restriction"),
    ("Upgrade your trial account to send media.", "trial_restriction"),
    ("Trial account upgrade is required.", "trial_restriction"),
    ("The recipient is not verified.", "recipient_not_verified"),
    ("The sender is not enabled for WhatsApp.", "sender_not_enabled"),
    ("Outside the 24-hour customer service window.", "outside_window"),
    ("Failed to fetch media.", "media_fetch_or_type_rejected"),
    ("Unsupported media type.", "media_fetch_or_type_rejected"),
])
def test_explicit_english_reason_is_reduced_to_fixed_category(message, expected):
    assert classify(message)[0] == expected


@pytest.mark.parametrize("message", [
    "ContentSid is not required.",
    "MediaUrl is supported.",
    "MediaUrl is not unsupported.",
    "This is not a trial restriction.",
    "Trial accounts can send custom messages.",
    "The recipient is verified.",
    "The recipient is not unverified.",
    "The sender is enabled for WhatsApp.",
    "Not outside the 24-hour customer service window.",
    "Media fetch did not fail.",
    "The media type is not unsupported.",
    "ContentSid is required. ContentSid is not required.",
    "MediaUrl is not supported. MediaUrl is supported.",
    "ContentSid is required; MediaUrl is not supported.",
    "The recipient is not verified. The sender is not enabled for WhatsApp.",
    "Trial accounts cannot send custom messages. Failed to fetch media.",
    "This operation is restricted to trial accounts.",
    "ContentSid isn't required.",
    "MediaUrl isn't unsupported.",
    "If ContentSid is required, supply it.",
    "If MediaUrl is not supported, retry another way.",
    "It is not true that ContentSid is required.",
    "It is not the case that MediaUrl is not supported.",
    "ContentSid is required, but it isn't required for this request.",
    "The recipient is not verified, but this is not the reason for failure.",
    "No ContentSid is required.",
    "No recipient is unverified.",
    "It is false that customer service window has expired.",
    "ContentSid is required only if you use a template.",
    "Not failed to fetch media.",
    "It is not correct that ContentSid is required.",
    "Not an unverified recipient.",
    "Never upgrade your trial account to send media.",
    "Invalid request.", "Provider rejected the request.", "Forbidden.", "", PRIVATE,
    "The recipient, sender, trial, media, and window are mentioned without a reason.",
])
def test_negation_contradiction_multiple_or_unknown_reason_is_unclassified(message):
    assert classify(message)[0] == "unclassified"


@pytest.mark.parametrize("value", [None, 0, True, [], {}, b"ContentSid is required.",
    "ContentSid is required. " + "x" * 2048])
def test_non_string_or_over_limit_input_is_not_classified_or_extracted(value):
    assert classify(value) == ("unclassified", ())


def test_inclusive_2048_character_boundary():
    message = "ContentSid is required."
    category, params = classify(message + " " * (2048 - len(message)))
    assert category == "required_contentsid" and params == ("ContentSid",)


@pytest.mark.parametrize("message,expected", [
    ("MediaUrl ContentSid StatusCallback", {"MediaUrl", "ContentSid", "StatusCallback"}),
    ("'From' 'To' 'Body'", {"From", "To", "Body"}),
    ('"From" "To" "Body"', {"From", "To", "Body"}),
    ("parameter From; parameter To; parameter Body", {"From", "To", "Body"}),
    ("From the service To the Body of the response", set()),
    ("MediaUrlX xContentSid StatusCallbackOther ParameterFrom ToBody", set()),
    ("mediaurl contentsid statuscallback 'from' 'to' 'body'", set()),
    ("'AccountSid' 'AuthToken' 'MessagingServiceSid' 'Password'", set()),
    ("MediaUrl MediaUrl 'From' parameter From", {"MediaUrl", "From"}),
])
def test_parameter_mentions_are_exact_allowlisted_and_deduplicated(message, expected):
    assert set(classify(message)[1]) == expected


def test_parameter_tuple_and_diagnostic_use_stable_sorted_names_only():
    message = "'To' MediaUrl 'From' ContentSid 'Body' StatusCallback"
    expected = tuple(sorted(ALLOWED_PARAMS))
    assert classify(message) == ("unclassified", expected)
    error = TwilioRequestRejected(403, provider_message=message)
    assert error.parameter_mentions == expected
    assert error.safe_diagnostic_code == "twilio_http_403_category_unclassified_params_" + "+".join(expected)


@pytest.mark.parametrize("message,category,params", [
    ("ContentSid is required.", "required_contentsid", ("ContentSid",)),
    ("MediaUrl is not supported.", "unsupported_mediaurl", ("MediaUrl",)),
    ("The recipient is not verified.", "recipient_not_verified", ()),
    ("Please inspect parameter Body.", "unclassified", ("Body",)),
    ("Forbidden.", "unclassified", ()),
])
def test_exception_keeps_numeric_backcompat_and_bounded_safe_diagnostic(message, category, params):
    error = TwilioRequestRejected(403, 20008, provider_message=message)
    assert str(error) == error.safe_error_code == "twilio_http_403_code_20008"
    assert error.provider_error_category == category and error.parameter_mentions == params
    suffix = f"_category_{category}_params_{'+'.join(params) or 'none'}" if category != "unclassified" or params else ""
    assert error.safe_diagnostic_code == error.safe_error_code + suffix
    assert re.fullmatch(r"[a-zA-Z0-9_+]+", error.safe_diagnostic_code)


def assert_private_not_retained(error):
    representations = str(error) + repr(error) + repr(vars(error)) + "".join(traceback.format_exception(error))
    for value in PRIVATE_PARTS:
        assert value not in representations
    for name in ("body", "response", "request", "headers", "url", "message", "provider_message", "more_info"):
        assert not hasattr(error, name)


@pytest.mark.parametrize("category,params,expected_category,expected_params", [
    (PRIVATE, (PRIVATE,), "unclassified", ()),
    ("required_contentsid", (PRIVATE, "ContentSid", "ContentSid"), "required_contentsid", ("ContentSid",)),
    ("outside_window", ["Body", PRIVATE], "outside_window", ()),
    ({"reason": PRIVATE}, {"Body": PRIVATE}, "unclassified", ()),
    (True, (None, True, 20008, CAPABILITY), "unclassified", ()),
])
def test_constructor_revalidates_altered_classifier_output(monkeypatch, category, params, expected_category, expected_params):
    monkeypatch.setattr("services.twilio_provider._classify_rejection_message", lambda value: (category, params))
    error = TwilioRequestRejected(403, 20008, provider_message=PRIVATE)
    assert error.provider_error_category == expected_category
    assert error.parameter_mentions == expected_params
    assert re.fullmatch(r"[a-zA-Z0-9_+]+", error.safe_diagnostic_code)
    assert str(error) == "twilio_http_403_code_20008"
    assert_private_not_retained(error)


@pytest.mark.parametrize("reason,expected", [("ContentSid is required.", "required_contentsid"),
    ("MediaUrl is not supported.", "unsupported_mediaurl"), ("Forbidden.", "unclassified")])
def test_untrusted_sensitive_error_prose_is_never_retained(reason, expected):
    raw = {"code": 20008, "message": reason + " " + PRIVATE, "more_info": CAPABILITY,
           "account_sid": PRIVATE_PARTS[2], "token": TOKEN, "phone": PRIVATE_PARTS[1]}
    provider = TwilioWhatsAppProvider(ACCOUNT, TOKEN, BUSINESS,
        transport=httpx.MockTransport(lambda request: httpx.Response(403, json=raw)))
    with pytest.raises(TwilioRequestRejected) as caught:
        provider.send_text(DEMO_SENDER, "Fictional local test", "classification-key")
    error = caught.value
    assert error.provider_error_category == expected
    assert error.safe_error_code == "twilio_http_403_code_20008"
    assert_private_not_retained(error)


@pytest.mark.parametrize("message", [None, 100, True, ["ContentSid is required."],
    {"message": "ContentSid is required."}, "MediaUrl is not supported. " + "x" * 2048])
def test_invalid_json_message_field_leaves_existing_numeric_diagnostic(message):
    provider = TwilioWhatsAppProvider(ACCOUNT, TOKEN, BUSINESS,
        transport=httpx.MockTransport(lambda request: httpx.Response(403, json={"code": 20008, "message": message})))
    with pytest.raises(TwilioRequestRejected) as caught:
        provider.send_text(DEMO_SENDER, "Fictional local test", "classification-key")
    error = caught.value
    assert error.provider_error_category == "unclassified" and error.parameter_mentions == ()
    assert error.safe_diagnostic_code == error.safe_error_code == "twilio_http_403_code_20008"


def test_other_json_fields_are_never_used_as_reason_text():
    provider = TwilioWhatsAppProvider(ACCOUNT, TOKEN, BUSINESS,
        transport=httpx.MockTransport(lambda request: httpx.Response(403, json={"code": 20008,
            "message": "Forbidden.", "details": "ContentSid is required.", "more_info": "MediaUrl is not supported."})))
    with pytest.raises(TwilioRequestRejected) as caught:
        provider.send_text(DEMO_SENDER, "Fictional local test", "classification-key")
    assert caught.value.safe_diagnostic_code == "twilio_http_403_code_20008"


def test_invalid_numeric_code_cannot_contaminate_classification():
    provider = TwilioWhatsAppProvider(ACCOUNT, TOKEN, BUSINESS,
        transport=httpx.MockTransport(lambda request: httpx.Response(403, json={"code": PRIVATE,
            "message": "ContentSid is required."})))
    with pytest.raises(TwilioRequestRejected) as caught:
        provider.send_text(DEMO_SENDER, "Fictional local test", "classification-key")
    assert caught.value.safe_diagnostic_code == "twilio_http_403_category_required_contentsid_params_ContentSid"
    assert_private_not_retained(caught.value)


@pytest.mark.parametrize("status,kind", [(429, RetryableProviderError), (408, AmbiguousSendError), (503, AmbiguousSendError)])
def test_classifiable_body_never_changes_retry_or_uncertainty_semantics(status, kind):
    provider = TwilioWhatsAppProvider(ACCOUNT, TOKEN, BUSINESS,
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json={"code": 20008,
            "message": "ContentSid is required. " + PRIVATE})))
    with pytest.raises(kind) as caught:
        provider.send_text(DEMO_SENDER, "Fictional local test", "classification-key")
    assert not isinstance(caught.value, TwilioRequestRejected)
    assert_private_not_retained(caught.value)


def assert_pdf_outbox_persists_only_classification_once(tmp_path, reason, suffix):
    store, files = WorkflowStore(tmp_path / "state.sqlite3"), PrivateFileStore(tmp_path / "private")
    phone = twilio_business_id(ACCOUNT, BUSINESS)
    store.enroll_tenant(tenant_id="synthetic", manager_id="gm", hotel_code=DEMO_CODE, hotel_name="Fictional Classifier Hotel")
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
        return httpx.Response(403, json={"code": 20008, "message": reason + " " + PRIVATE, "more_info": CAPABILITY})
    provider = TwilioWhatsAppProvider(ACCOUNT, TOKEN, BUSINESS, media_publisher=Publisher(), transport=httpx.MockTransport(transport))
    worker = ForecastWorker(store, files, provider, clock=lambda: clock[0])
    store.ingest_twilio({"AccountSid": ACCOUNT, "MessageSid": MESSAGE, "From": "whatsapp:+" + DEMO_SENDER,
        "To": "whatsapp:+" + BUSINESS, "NumMedia": "1", "MediaContentType0": "application/pdf", "MediaUrl0": MEDIA_URL},
        clock[0], account_sid=ACCOUNT, business_sender=BUSINESS)
    assert worker.drain_ready() == {"processed": 1, "dispatched": 1}
    item = store.rows("wa_outbox")[0]
    assert item["status"] == "failed" and item["attempts"] == 1
    assert item["kind"] == "document" and item["provider_message_id"] is None
    assert item["error_code"] == "twilio_http_403_code_20008" + suffix
    assert len(store.list_weekly_roster_versions("gm")) == 1
    clock[0] += 3600
    assert worker.drain_ready() == {"processed": 0, "dispatched": 0} and sends == ["POST"]
    with store._connect() as db:
        dump = "\n".join(db.iterdump())
    assert all(value not in dump for value in PRIVATE_PARTS)


@pytest.mark.parametrize("reason,suffix", [
    ("ContentSid is required.", "_category_required_contentsid_params_ContentSid"),
    ("MediaUrl is not supported.", "_category_unsupported_mediaurl_params_MediaUrl"),
    ("Unknown private failure.", ""),
])
def test_synthetic_pdf_outbox_persists_only_classification_once(tmp_path, reason, suffix):
    assert_pdf_outbox_persists_only_classification_once(tmp_path, reason, suffix)


def test_altered_classifier_cannot_persist_sensitive_output(tmp_path, monkeypatch):
    monkeypatch.setattr("services.twilio_provider._classify_rejection_message", lambda value: (PRIVATE, (PRIVATE, TOKEN)))
    assert_pdf_outbox_persists_only_classification_once(tmp_path, "ContentSid is required.", "")
