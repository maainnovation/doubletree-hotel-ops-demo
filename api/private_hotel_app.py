"""Authenticated LOCAL development view over the worker's exact injected store.

No default DB, tenant, token, demo data, or network provider is created here.
Token-map auth is an explicit pilot adapter, not production SSO. Bind a local
runner to 127.0.0.1; real deployment still requires approved private HTTPS,
identity lifecycle/roles, rate limits, audit/retention and production storage.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from services.hotel_parameters import apply_shared_parameters, shared_parameters_from_staff
from services.staff_settings import clean_payload, validate_staff_settings
from services.weekly_roster import build_exports, roster_calculation_summary


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
STATIC = Path(__file__).with_name("private_app_static")
MAX_BODY = 2 * 1024 * 1024
BODY_TIMEOUT_SECONDS = 30
CALCULATION_SCOPE = {
    "weekly_cleaning_demand": "departures × checkout minutes + all stayovers × stayover minutes",
    "productive_shift_time": "paid shift time minus breaks and other duties",
    "inspection_buffer_in_weekly_demand": False,
    "stayover_policy_text_filters_counts": False,
    "note": "Inspection buffer is not included in weekly demand. Stayover policy text is descriptive; it does not filter forecast counts.",
}


@dataclass(frozen=True)
class HotelPrincipal:
    tenant_id: str
    manager_id: str

    def __post_init__(self):
        if not all(isinstance(value, str) and _ID.fullmatch(value) for value in (self.tenant_id, self.manager_id)):
            raise ValueError("Explicit tenant and manager mappings are required")


class TokenMapAuthenticator:
    """Trusted server configuration only; token digests are kept in this object.

    Configuration must come from an authorized operator/secret manager. This
    class never creates accounts/tokens and never accepts a tenant from a client.
    Empty configuration denies all requests. Do not use test tokens in a pilot.
    """
    def __init__(self, mapping: dict[str, HotelPrincipal]):
        if not isinstance(mapping, dict) or len(mapping) > 100:
            raise ValueError("Invalid private authentication configuration")
        self._entries = []
        for token, principal in mapping.items():
            if (not isinstance(token, str) or not 24 <= len(token) <= 512
                    or not token.isascii() or any(char.isspace() for char in token)
                    or not isinstance(principal, HotelPrincipal)):
                raise ValueError("Invalid private authentication configuration")
            self._entries.append((hashlib.sha256(token.encode()).digest(), principal))

    def __call__(self, token: str) -> HotelPrincipal | None:
        if not isinstance(token, str) or not token.isascii() or len(token) > 512:
            return None
        digest = hashlib.sha256(token.encode()).digest()
        found = None
        for expected, principal in self._entries:
            if hmac.compare_digest(digest, expected):
                found = principal
        return found

    @classmethod
    def from_environment(cls, variable="PRIVATE_HOTEL_TOKENS_JSON"):
        """Optional server-only secret input; unset means deny, never auto-enroll.

        Shape: [{"token":"<operator-provided-token>","tenant_id":"...",
                 "manager_id":"..."}]. No concrete credential is shipped.
        """
        raw = os.getenv(variable, "")
        if not raw:
            return cls({})
        if len(raw) > 65536:
            raise ValueError("Invalid private authentication configuration")
        try:
            def unique_object(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError
                    result[key] = value
                return result
            entries = json.loads(raw, object_pairs_hook=unique_object)
            if not isinstance(entries, list) or len(entries) > 100:
                raise ValueError
            mapping = {}
            for item in entries:
                if not isinstance(item, dict) or set(item) != {"token", "tenant_id", "manager_id"} or item["token"] in mapping:
                    raise ValueError
                mapping[item["token"]] = HotelPrincipal(item["tenant_id"], item["manager_id"])
            return cls(mapping)
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError("Invalid private authentication configuration") from None


class ExpiringTokenMapAuthenticator(TokenMapAuthenticator):
    """Operator-supplied owner key, bounded by the injected approved window.

    No key is generated. The maximum supported owner pilot is 240 hours;
    deployment supplies its actual activation and expiry, not browser values.
    """
    def __init__(self, mapping, *, issued_at, expires_at, clock=time.time):
        if (any(type(value) not in {int, float} or not math.isfinite(value) for value in (issued_at, expires_at))
                or not 0 < expires_at - issued_at <= 240 * 3600):
            raise ValueError("An explicit owner access window of at most 240 hours is required")
        super().__init__(mapping)
        self.issued_at, self.expires_at, self.clock = issued_at, expires_at, clock

    def __call__(self, token):
        now = self.clock()
        if type(now) not in {int, float} or not math.isfinite(now) or not self.issued_at <= now < self.expires_at:
            return None
        return super().__call__(token)


class UploadAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: StrictInt = Field(ge=1)
    request_key: str | None = Field(default=None, min_length=8, max_length=80)


class ParameterEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: dict = Field(min_length=1, max_length=3)
    expected_revision: StrictInt | None = Field(default=None, ge=1)


class StaffEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    staff: list[dict] = Field(min_length=1, max_length=500)
    expected_revision: StrictInt | None = Field(default=None, ge=1)


def _precondition(expected: int | None, if_match: str | None) -> int:
    header_revision = None
    if if_match is not None:
        match = re.fullmatch(r'"?([1-9][0-9]{0,17})"?', if_match)
        if not match or (if_match.startswith('"') != if_match.endswith('"')):
            raise HTTPException(400, "If-Match must contain one settings revision")
        header_revision = int(match.group(1))
    if expected is None and header_revision is None:
        raise HTTPException(428, "A settings revision is required; reload before saving")
    if expected is not None and header_revision is not None and expected != header_revision:
        raise HTTPException(400, "Revision preconditions disagree")
    return expected if expected is not None else header_revision


def _safe_roster(roster):
    """Allowlist views; never return worker job IDs, media keys or storage paths."""
    keys = ("week_start", "week_end", "version", "updated_at", "feasible", "forecast", "schedule", "constraints", "demand_minutes")
    result = {key: deepcopy(roster[key]) for key in keys if key in roster}
    source = roster.get("source") if isinstance(roster.get("source"), dict) else {}
    result["source"] = {key: source[key] for key in ("channel", "hotel_name", "report_profile", "created_at") if key in source}
    result["calculations"] = roster_calculation_summary(roster)
    result["calculation_scope"] = dict(CALCULATION_SCOPE)
    return result


def _summary(roster):
    safe = _safe_roster(roster)
    return {key: safe[key] for key in ("week_start", "week_end", "version", "updated_at", "feasible", "source") if key in safe}


def create_private_app(store, files, authenticator: Callable[[str], HotelPrincipal | None] | None = None, *, forecast_workflow=None) -> FastAPI:
    """Use precisely the supplied worker store/files, with fail-closed auth."""
    app = FastAPI(title="Private hotel development workspace", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.workflow_store = store
    app.state.private_files = files  # Never mounted or exposed as a public route.
    if forecast_workflow is not None and (forecast_workflow.store is not store or forecast_workflow.files is not files):
        raise ValueError("Private upload workflow must use the same injected store and files")
    app.state.forecast_workflow = forecast_workflow
    # Deployment invariant: exactly ONE API process/replica. This process-local
    # intake gate is not a distributed concurrency limiter.
    upload_intake = asyncio.Lock()
    authenticate = authenticator or (lambda token: None)

    def resolve(request):
        headers = request.headers.getlist("authorization")
        if len(headers) != 1 or not headers[0].startswith("Bearer "):
            raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})
        token = headers[0][7:]
        if not token or len(token) > 512 or any(char.isspace() for char in token):
            raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})
        try:
            principal = authenticate(token)
        except Exception:
            # Do not echo credentials, config or authentication-provider details.
            raise HTTPException(401, "Authentication required") from None
        if not isinstance(principal, HotelPrincipal):
            raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})
        with store._connect() as db:
            row = db.execute("SELECT hotel_name FROM wa_tenants WHERE tenant_id=? AND manager_id=? AND enabled=1", (principal.tenant_id, principal.manager_id)).fetchone()
        if not row:
            raise HTTPException(403, "Hotel access is unavailable")
        request.state.principal = principal
        request.state.hotel_name = row[0]

    @app.middleware("http")
    async def private_boundary(request: Request, call_next):
        intake_owned = False
        try:
            if request.url.path.startswith("/private/api"):
                resolve(request)  # Authentication precedes parsing any private request body.
                if request.method in {"POST", "PUT", "PATCH"}:
                    limit = MAX_BODY
                    if request.url.path.startswith("/private/api/forecast-uploads"):
                        if forecast_workflow is None:
                            raise HTTPException(503, "Private forecast uploads are not configured")
                        who = request.state.principal
                        if not forecast_workflow.available(who.tenant_id, who.manager_id):
                            raise HTTPException(403, "Owner upload approval or synthetic hotel setup is unavailable")
                    if request.method == "POST" and request.url.path == "/private/api/forecast-uploads":
                        mime = request.headers.get("content-type", "")
                        if mime not in {"application/pdf", "image/png", "image/jpeg"}:
                            raise HTTPException(415, "Use a PDF, PNG or JPEG")
                        limit = (10 if mime == "application/pdf" else 5) * 1024 * 1024
                        # Reject excess concurrent bodies, rather than accumulating
                        # an unbounded in-memory wait queue on a small pilot host.
                        if upload_intake.locked():
                            raise HTTPException(429, "Another upload is being received; try again after it completes")
                        await upload_intake.acquire()
                        intake_owned = True
                    async def bounded_body():
                        chunks, size = [], 0
                        async for chunk in request.stream():
                            size += len(chunk)
                            if size > limit:
                                raise HTTPException(413, "Request body exceeds the private upload limit")
                            chunks.append(chunk)
                        return b"".join(chunks)
                    try:
                        request._body = await asyncio.wait_for(bounded_body(), timeout=BODY_TIMEOUT_SECONDS)
                    except TimeoutError:
                        raise HTTPException(408, "Upload took too long; resend the file") from None
                    resolve(request)  # A slow body cannot outlive owner-key expiry.
            response = await call_next(request)
        except HTTPException as exc:
            response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
        finally:
            if intake_owned:
                upload_intake.release()
        response.headers.update({
            "Cache-Control": "no-store", "Pragma": "no-cache", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        })
        return response

    def principal(request: Request):
        value = getattr(request.state, "principal", None)
        if not isinstance(value, HotelPrincipal):
            raise HTTPException(401, "Authentication required")
        return value

    def saved_settings(who):
        saved = store.get_staff_settings(who.manager_id)
        if not saved:
            raise HTTPException(404, "Hotel settings are not configured")
        saved["dataset_mode"] = saved["_dataset_mode"]
        return saved

    def settings_response(saved):
        return {"revision": saved["_settings_revision"], "parameters": shared_parameters_from_staff(saved),
                "staff": deepcopy(saved["staff"]), "settings": deepcopy(saved["settings"]),
                "dataset_mode": saved["_dataset_mode"], "development_only": bool(getattr(store, "development_only", True)),
                "calculation_scope": dict(CALCULATION_SCOPE)}

    def set_revision(response, revision):
        response.headers["ETag"] = f'"{revision}"'

    def validate_week(week_start):
        try:
            if date.fromisoformat(week_start).isoformat() != week_start:
                raise ValueError
        except ValueError:
            raise HTTPException(422, "Use an ISO week-start date") from None

    def find_roster(who, week_start, version=None):
        validate_week(week_start)
        if version is None:
            roster = store.get_weekly_roster(who.manager_id, week_start)
        else:
            roster = next((item for item in store.list_weekly_roster_versions(who.manager_id, week_start) if item["version"] == version), None)
        if not roster:
            raise HTTPException(404, "Roster version not found")
        return roster

    @app.get("/private", include_in_schema=False)
    @app.get("/private/", include_in_schema=False)
    def shell():
        return FileResponse(STATIC / "index.html", media_type="text/html")

    @app.get("/private/assets/{filename}", include_in_schema=False)
    def asset(filename: str):
        if filename not in {"app.js", "style.css"}:
            raise HTTPException(404, "Not found")
        return FileResponse(STATIC / filename, media_type="application/javascript" if filename.endswith(".js") else "text/css")

    @app.get("/private/api/settings")
    def settings(request: Request, response: Response, who=Depends(principal)):
        saved = saved_settings(who)
        set_revision(response, saved["_settings_revision"])
        return {"hotel_name": request.state.hotel_name, "forecast_uploads_available": bool(forecast_workflow and forecast_workflow.available(who.tenant_id, who.manager_id)), **settings_response(saved)}

    @app.get("/private/api/parameters")
    def parameters(response: Response, who=Depends(principal)):
        saved = saved_settings(who)
        set_revision(response, saved["_settings_revision"])
        return {"revision": saved["_settings_revision"], "parameters": shared_parameters_from_staff(saved),
                "calculation_scope": dict(CALCULATION_SCOPE)}

    @app.patch("/private/api/parameters")
    def edit_parameters(body: ParameterEdit, response: Response, if_match: str | None = Header(default=None), who=Depends(principal)):
        revision = _precondition(body.expected_revision, if_match)
        current = saved_settings(who)
        if current["_settings_revision"] != revision:
            raise HTTPException(409, "Settings changed; reload before saving")
        try:
            candidate = apply_shared_parameters(current, body.values)
            errors = validate_staff_settings(candidate)
            if errors:
                raise ValueError("Saved staff/settings need correction before this change")
        except (ValueError, TypeError, KeyError):
            raise HTTPException(422, "Invalid shared hotel parameters") from None
        try:
            saved = store.save_hotel_parameters(who.manager_id, body.values, expected_revision=revision)
        except ValueError:
            raise HTTPException(409, "Settings changed; reload before saving") from None
        set_revision(response, saved["_settings_revision"])
        return settings_response(saved)

    @app.get("/private/api/staff")
    def staff(response: Response, who=Depends(principal)):
        saved = saved_settings(who)
        set_revision(response, saved["_settings_revision"])
        return {"revision": saved["_settings_revision"], "staff": deepcopy(saved["staff"])}

    @app.patch("/private/api/staff")
    def edit_staff(body: StaffEdit, response: Response, if_match: str | None = Header(default=None), who=Depends(principal)):
        revision = _precondition(body.expected_revision, if_match)
        current = saved_settings(who)
        if current["_settings_revision"] != revision:
            raise HTTPException(409, "Settings changed; reload before saving")
        candidate = clean_payload(current)
        candidate["staff"] = deepcopy(body.staff)
        try:
            errors = validate_staff_settings(candidate)
        except (ValueError, TypeError, KeyError, AttributeError):
            errors = ["Invalid staff rows"]
        if errors:
            raise HTTPException(422, "Invalid staff rows, availability, or hour limits")
        try:
            new_revision = store.save_staff_settings(who.manager_id, candidate,
                demo_seed_version=current["_demo_seed_version"], dataset_mode=current["_dataset_mode"], expected_revision=revision)
        except ValueError:
            raise HTTPException(409, "Settings changed; reload before saving") from None
        saved = dict(candidate, _settings_revision=new_revision, _dataset_mode=current["_dataset_mode"])
        set_revision(response, new_revision)
        return settings_response(saved)

    def upload_service():
        if forecast_workflow is None:
            raise HTTPException(503, "Private forecast uploads are not configured")
        return forecast_workflow

    def upload_call(method, *args, **kwargs):
        from services.private_forecast_workflow import PrivateForecastError
        from services.private_storage_budget import PrivateStorageQuotaError
        from services.whatsapp_forecast import ForecastNeedsCorrection
        try:
            return method(*args, **kwargs)
        except PrivateForecastError as exc:
            raise HTTPException(exc.status_code, exc.detail) from None
        except PrivateStorageQuotaError:
            raise HTTPException(507, "Private storage quota reached. Contact the operator; do not repeatedly resend the file.") from None
        except ForecastNeedsCorrection:
            raise HTTPException(409, "The candidate is no longer valid; upload the current complete report again") from None

    @app.post("/private/api/forecast-uploads", status_code=202)
    async def upload_forecast(request: Request, who=Depends(principal)):
        service = upload_service()
        return upload_call(service.enqueue, who.tenant_id, who.manager_id, await request.body(),
                           request.headers.get("content-type", ""), request.headers.get("idempotency-key"))

    @app.get("/private/api/forecast-uploads")
    def list_uploads(who=Depends(principal)):
        service = upload_service()
        return {"uploads": upload_call(service.list_uploads, who.tenant_id, who.manager_id)}

    @app.get("/private/api/forecast-uploads/{upload_id}")
    def get_upload(upload_id: str, who=Depends(principal)):
        service = upload_service()
        return upload_call(service.get, who.tenant_id, who.manager_id, upload_id)

    @app.post("/private/api/forecast-uploads/{upload_id}/{action}")
    def upload_action(upload_id: str, action: str, body: UploadAction, who=Depends(principal)):
        service = upload_service()
        if action not in {"confirm", "cancel", "regenerate"}:
            raise HTTPException(404, "Not found")
        return upload_call(service.action, who.tenant_id, who.manager_id, upload_id, action,
                           body.expected_revision, body.request_key)

    @app.get("/private/api/rosters")
    def rosters(who=Depends(principal)):
        versions = store.list_weekly_roster_versions(who.manager_id)
        latest = {}
        for item in versions:
            key = item["week_start"]
            if key not in latest or item["version"] > latest[key]["version"]:
                latest[key] = item
        return {"latest": [_summary(item) for item in latest.values()], "versions": [_summary(item) for item in versions]}

    @app.get("/private/api/rosters/{week_start}")
    def current_roster(week_start: str, who=Depends(principal)):
        return _safe_roster(find_roster(who, week_start))

    @app.get("/private/api/rosters/{week_start}/versions/{version}")
    def roster_version(week_start: str, version: int, who=Depends(principal)):
        if version < 1:
            raise HTTPException(422, "Invalid roster version")
        return _safe_roster(find_roster(who, week_start, version))

    @app.get("/private/api/rosters/{week_start}/versions/{version}/pdf")
    def roster_pdf(week_start: str, version: int, who=Depends(principal)):
        if version < 1:
            raise HTTPException(422, "Invalid roster version")
        roster = find_roster(who, week_start, version)
        _, pdf = build_exports(roster)
        return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="weekly-roster-{week_start}-v{version}.pdf"'})

    return app
