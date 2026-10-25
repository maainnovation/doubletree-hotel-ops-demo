"""Transport tests use httpx.MockTransport exclusively: no live Meta calls."""
import json

import httpx
import pytest

from services.whatsapp_provider import (AmbiguousSendError, FakeWhatsAppProvider,
    MetaWhatsAppProvider, ProviderError, RetryableProviderError, provider_from_env)


PHONE = "15555550123"
PDF = b"%PDF-1.4\nsynthetic test only\n%%EOF"
URL = "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=123"


def meta(handler):
    return MetaWhatsAppProvider("test-only-secret", "987", "v25.0", production_approved=True,
                                transport=httpx.MockTransport(handler))


def test_default_fake_never_uses_credentials_or_network():
    provider = provider_from_env({"WHATSAPP_ACCESS_TOKEN": "not-a-real-token"})
    assert isinstance(provider, FakeWhatsAppProvider)
    assert provider.is_live is False


@pytest.mark.parametrize("config", [
    {"WHATSAPP_PROVIDER": "typo"},
    {"WHATSAPP_PROVIDER": "meta"},
    {"WHATSAPP_PROVIDER": "meta", "WHATSAPP_PRODUCTION_APPROVED": "1"},
])
def test_factory_requires_explicit_complete_live_configuration(config):
    with pytest.raises(ProviderError):
        provider_from_env(config)


def test_factory_complete_configuration_does_not_make_a_request(monkeypatch):
    def no_network(*a, **kw):
        raise AssertionError("No network during construction")
    monkeypatch.setattr(httpx, "Client", no_network)
    config = {"WHATSAPP_PROVIDER": "meta", "WHATSAPP_PRODUCTION_APPROVED": "1",
              "WHATSAPP_ACCESS_TOKEN": "synthetic", "WHATSAPP_PHONE_NUMBER_ID": "987",
              "META_GRAPH_API_VERSION": "v25.0", "META_APP_SECRET": "synthetic",
              "WHATSAPP_VERIFY_TOKEN": "synthetic"}
    assert provider_from_env(config).is_live is True


def test_direct_live_construction_is_gated():
    with pytest.raises(ProviderError):
        MetaWhatsAppProvider("synthetic", "987", "v25.0")
    with pytest.raises(ProviderError):
        MetaWhatsAppProvider("synthetic", "987", "v25.0/../../", production_approved=True)


def test_fake_media_and_same_key_are_deterministic():
    provider = FakeWhatsAppProvider(media={"fixture": (PDF, "application/pdf")})
    assert provider.fetch_media("fixture", "application/pdf", 1024) == PDF
    first = provider.send_document(PHONE, PDF, "roster.pdf", "Needs attention", "outbox:1")
    assert first == provider.send_document(PHONE, PDF, "roster.pdf", "Needs attention", "outbox:1")
    assert len(provider.sent) == 1
    assert provider.sent[0]["to"] == PHONE
    assert provider.sent[0]["pdf_bytes"] == PDF
    with pytest.raises(ProviderError):
        provider.send_text(PHONE, "different payload", "outbox:1")


@pytest.mark.parametrize("mime,limit", [("image/jpeg", 1024), ("application/pdf", 1),
                                         ("text/html", 1024), ("application/pdf", True)])
def test_fake_media_type_and_size_validation(mime, limit):
    with pytest.raises(ProviderError):
        FakeWhatsAppProvider(media={"fixture": (PDF, "application/pdf")}).fetch_media("fixture", mime, limit)


def test_meta_fetch_binds_phone_validates_type_and_streams():
    seen = []
    def handler(request):
        seen.append(request)
        assert request.headers["authorization"] == "Bearer test-only-secret"
        if request.url.host == "graph.facebook.com":
            assert request.url.path == "/v25.0/123"
            assert request.url.params["phone_number_id"] == "987"
            return httpx.Response(200, json={"id": "123", "mime_type": "application/pdf", "file_size": len(PDF), "url": URL})
        return httpx.Response(200, content=PDF, headers={"Content-Type": "application/pdf"})
    assert meta(handler).fetch_media("123", "application/pdf", 1024) == PDF
    assert len(seen) == 2


@pytest.mark.parametrize("bad_url", [
    "http://lookaside.fbsbx.com/whatsapp_business/attachments/",
    "https://lookaside.fbsbx.com.evil.example/whatsapp_business/attachments/",
    "https://lookaside.fbsbx.com@evil.example/whatsapp_business/attachments/",
    "https://127.0.0.1/whatsapp_business/attachments/",
    "https://lookaside.fbsbx.com:444/whatsapp_business/attachments/",
    "https://lookaside.fbsbx.com/not-a-media-endpoint",
    "file:///etc/passwd",
])
def test_meta_rejects_untrusted_download_before_sending_token(bad_url):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"id": "123", "mime_type": "application/pdf", "file_size": len(PDF), "url": bad_url})
    with pytest.raises(ProviderError):
        meta(handler).fetch_media("123", "application/pdf", 1024)
    assert len(seen) == 1


def test_meta_does_not_follow_download_redirects():
    seen = []
    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(200, json={"id": "123", "mime_type": "application/pdf", "file_size": len(PDF), "url": URL})
        return httpx.Response(302, headers={"Location": "https://evil.example/"})
    with pytest.raises(ProviderError):
        meta(handler).fetch_media("123", "application/pdf", 1024)
    assert len(seen) == 2


@pytest.mark.parametrize("metadata", [
    {"id": "999"}, {"mime_type": "image/png"}, {"file_size": None},
    {"file_size": 2048}, {"file_size": True}, {"file_size": "1.5"},
])
def test_meta_rejects_mismatched_or_unbounded_metadata(metadata):
    seen = []
    def handler(request):
        seen.append(request)
        data = {"id": "123", "mime_type": "application/pdf", "file_size": len(PDF), "url": URL}
        return httpx.Response(200, json={**data, **metadata})
    with pytest.raises(ProviderError):
        meta(handler).fetch_media("123", "application/pdf", 1024)
    assert len(seen) == 1


@pytest.mark.parametrize("headers,content", [
    ({"Content-Type": "text/html"}, PDF),
    ({"Content-Type": "application/pdf"}, b"x" * 2048),
    ({"Content-Type": "application/pdf"}, PDF + b"x"),
])
def test_meta_checks_download_content_type_and_actual_size(headers, content):
    def handler(request):
        if request.url.host == "graph.facebook.com":
            return httpx.Response(200, json={"id": "123", "mime_type": "application/pdf", "file_size": len(PDF), "url": URL})
        return httpx.Response(200, headers=headers, content=content)
    with pytest.raises(ProviderError):
        meta(handler).fetch_media("123", "application/pdf", 1024)


def test_meta_document_upload_then_send_protocol():
    seen = []
    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/media"):
            assert b'name="messaging_product"' in request.content
            assert b'application/pdf' in request.content
            assert PDF in request.content
            return httpx.Response(200, json={"id": "456"})
        body = json.loads(request.content)
        assert body == {"messaging_product": "whatsapp", "to": PHONE, "type": "document",
                        "document": {"id": "456", "filename": "roster.pdf", "caption": "Needs attention"}}
        assert "idempotency-key" not in request.headers
        return httpx.Response(200, json={"messages": [{"id": "wamid.synthetic"}]})
    assert meta(handler).send_document(PHONE, PDF, "roster.pdf", "Needs attention", "outbox:1") == "wamid.synthetic"
    assert [r.url.path for r in seen] == ["/v25.0/987/media", "/v25.0/987/messages"]


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.WriteError, httpx.ReadError])
def test_uncertain_sends_never_classified_retryable(failure):
    calls = []
    def handler(request):
        calls.append(request)
        raise failure("secret body must not escape", request=request)
    with pytest.raises(AmbiguousSendError) as error:
        meta(handler).send_text(PHONE, "Synthetic reply", "outbox:1")
    assert len(calls) == 1
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("status,body", [(500, {}), (503, {}), (408, {}), (200, {}), (200, {"messages": "wrong"})])
def test_uncertain_acknowledgements_require_reconciliation(status, body):
    with pytest.raises(AmbiguousSendError):
        meta(lambda req: httpx.Response(status, json=body)).send_text(PHONE, "Synthetic reply", "outbox:1")


@pytest.mark.parametrize("body", [b"not JSON", b"\xff", b'{"messages": [', b"[1, 2]", b"x" * 70000])
def test_invalid_success_body_is_ambiguous_not_retryable(body):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=body)
    with pytest.raises(AmbiguousSendError):
        meta(handler).send_text(PHONE, "Synthetic reply", "outbox:1")
    assert len(calls) == 1


def test_provably_pre_send_connection_failure_is_retryable():
    def handler(request):
        raise httpx.ConnectTimeout("private details", request=request)
    with pytest.raises(RetryableProviderError):
        meta(handler).send_text(PHONE, "Synthetic reply", "outbox:1")


def test_upload_read_timeout_is_safe_to_retry_because_no_message_was_sent():
    def handler(request):
        assert request.url.path.endswith("/media")
        raise httpx.ReadTimeout("private details", request=request)
    with pytest.raises(RetryableProviderError):
        meta(handler).send_document(PHONE, PDF, "roster.pdf", "Synthetic", "outbox:1")


def test_rejected_sends_are_not_leaked_or_blindly_retried():
    with pytest.raises(ProviderError) as error:
        meta(lambda req: httpx.Response(400, json={"error": "private data"})).send_text(PHONE, "Synthetic", "outbox:1")
    assert not isinstance(error.value, RetryableProviderError)
    assert "private" not in str(error.value)


@pytest.mark.parametrize("to,text,key", [("+15555550123", "text", "key"), (PHONE, "", "key"),
    (PHONE, "x" * 4001, "key"), (PHONE, "text", "../bad"), (PHONE, "text", "")])
def test_invalid_outbound_arguments_fail_before_transport(to, text, key):
    def handler(request):
        raise AssertionError("Should not make an HTTP request")
    with pytest.raises(ProviderError):
        meta(handler).send_text(to, text, key)
