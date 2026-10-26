"""LOCAL composition of authenticated app, signed intake and separate worker.

The caller explicitly supplies one shared durable store, private file adapter,
provider and authenticator. Importing this module creates no app, DB, credentials
or network client. HTTP handlers only enqueue/record status: OCR, calculation
and sends run through the attached worker outside the request lifecycle.

This is not a production deployment approval. Development adapters remain
synthetic-only; private app authentication/storage and live provider hosting
still need their separate production gates. No public media or enrollment API
is added. Twilio form signing follows its official webhook security contract:
https://www.twilio.com/docs/usage/webhooks/webhooks-security
"""
from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import re
import time
from urllib.parse import parse_qsl, urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import Response

from api.private_hotel_app import create_private_app
from services.twilio_provider import twilio_business_id, validate_twilio_signature
from services.whatsapp_provider import ProviderError
from services.whatsapp_workflow import ForecastWorker


INBOUND_PATH = "/webhooks/twilio/whatsapp"
STATUS_PATH = "/webhooks/twilio/status"
MAX_FORM_BYTES = 65536


def _callback_parts(value, expected_path):
    if (not isinstance(value, str) or len(value) > 4096 or not value.isascii()
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
            or "\\" in value or re.search(r"%(?![0-9a-fA-F]{2})", value)):
        raise ValueError("Explicit canonical HTTPS callback URLs are required")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                or parsed.port is not None or parsed.fragment or parsed.path != expected_path
                or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", parsed.netloc)
                or "." not in parsed.netloc or parsed.netloc.endswith((".localhost", ".local", ".internal"))):
            raise ValueError
        try:
            ipaddress.ip_address(parsed.hostname)
        except ValueError:
            return parsed
    except (TypeError, ValueError):
        pass
    raise ValueError("Explicit canonical HTTPS callback URLs are required")


@dataclass(frozen=True)
class TwilioWebhookConfig:
    """Trusted server configuration, never inferred from request/proxy headers."""
    account_sid: str
    auth_token: str = field(repr=False)
    business_sender: str
    webhook_url: str
    status_url: str
    max_jobs: int = 100

    def __post_init__(self):
        try:
            business_id = twilio_business_id(self.account_sid, self.business_sender)
        except ProviderError:
            raise ValueError("Explicit valid Twilio account and business sender are required") from None
        if (not isinstance(self.auth_token, str) or not 1 <= len(self.auth_token) <= 256
                or not self.auth_token.isascii() or any(char.isspace() for char in self.auth_token)
                or any(ord(char) < 32 or ord(char) == 127 for char in self.auth_token)
                or type(self.max_jobs) is not int or not 1 <= self.max_jobs <= 100):
            raise ValueError("Valid explicit webhook credentials and bounded queue limit are required")
        inbound = _callback_parts(self.webhook_url, INBOUND_PATH)
        status = _callback_parts(self.status_url, STATUS_PATH)
        if inbound.netloc != status.netloc:
            raise ValueError("Callback URLs must use the same configured HTTPS origin")
        object.__setattr__(self, "business_sender", business_id.rsplit(":", 1)[1])

    @property
    def phone_number_id(self):
        return twilio_business_id(self.account_sid, self.business_sender)


def create_shared_workflow_app(store, files, provider, authenticator, webhook_config,
                               clock=time.time, *, media_evaluator=None):
    """Compose exact injected adapters; callers supervise the worker separately.

    ``app.state.worker.drain_ready()`` is exposed only as a process-local testing
    and runner interface, never as an HTTP route or background request task.
    Image/scanned-PDF candidates always require a same-sender confirmation.
    """
    if not isinstance(webhook_config, TwilioWebhookConfig):
        raise ValueError("Explicit TwilioWebhookConfig is required")
    if getattr(provider, "phone_number_id", None) != webhook_config.phone_number_id:
        raise ValueError("Worker provider must match the configured business account and number")
    worker = ForecastWorker(store, files, provider, clock=clock,
        acknowledge_uploads=True, review_images=True, media_evaluator=media_evaluator)
    app = create_private_app(store, files, authenticator)
    app.state.worker = worker

    async def signed_form(request, expected_url):
        content_types = request.headers.getlist("content-type")
        signatures = request.headers.getlist("x-twilio-signature")
        if len(content_types) != 1 or content_types[0].split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
            raise HTTPException(415, "Expected form-encoded webhook")
        if len(signatures) != 1:
            raise HTTPException(401, "Invalid webhook signature")
        configured = urlsplit(expected_url)
        # The externally configured URL is signed verbatim, including its query
        # spelling/order. Never reconstruct it from Host or X-Forwarded-*.
        if (request.scope.get("raw_path", b"") != configured.path.encode("ascii")
                or request.scope.get("query_string", b"") != configured.query.encode("ascii")):
            raise HTTPException(400, "Webhook path or query does not match its configured URL")
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_FORM_BYTES:
                raise HTTPException(413, "Webhook body exceeds the development limit")
            chunks.append(chunk)
        try:
            raw = b"".join(chunks).decode("utf-8", "strict")
            if re.search(r"%(?![0-9a-fA-F]{2})", raw):
                raise ValueError
            pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True,
                encoding="utf-8", errors="strict", max_num_fields=100)
            form = dict(pairs)
            if (not pairs or len(form) != len(pairs)
                    or any(not key or len(key) > 128 or len(value) > 8192 for key, value in pairs)):
                raise ValueError
        except (ValueError, UnicodeError):
            raise HTTPException(400, "Invalid webhook form") from None
        if not validate_twilio_signature(webhook_config.auth_token, expected_url, form, signatures[0]):
            raise HTTPException(401, "Invalid webhook signature")
        return form

    @app.post(INBOUND_PATH)
    async def inbound(request: Request):
        form = await signed_form(request, webhook_config.webhook_url)
        try:
            store.ingest_twilio(form, clock(), account_sid=webhook_config.account_sid,
                business_sender=webhook_config.business_sender, max_jobs=webhook_config.max_jobs)
        except (ValueError, TypeError, ProviderError):
            raise HTTPException(400, "Invalid or over-limit workflow message") from None
        return Response('<?xml version="1.0" encoding="UTF-8"?><Response/>', media_type="application/xml")

    @app.post(STATUS_PATH)
    async def status(request: Request):
        form = await signed_form(request, webhook_config.status_url)
        try:
            store.twilio_status(form, clock(), account_sid=webhook_config.account_sid,
                business_sender=webhook_config.business_sender)
        except (ValueError, TypeError, ProviderError):
            raise HTTPException(400, "Invalid workflow status") from None
        return Response(status_code=204)

    return app
