"""Private app tests use only literal fake credentials and synthetic tenants."""
from copy import deepcopy
from datetime import date
from io import BytesIO
import json

from fastapi.testclient import TestClient
from pypdf import PdfReader
import pytest

from api.private_hotel_app import HotelPrincipal, TokenMapAuthenticator, create_private_app
from scripts.demo_whatsapp_workflow import DEMO_CODE, DEMO_NOW, DEMO_PHONE_ID, DEMO_SENDER, forecast_pdf, webhook_payload
from services.forecast_import import empty_forecast
from services.hotel_parameters import saved_room_capacity
from services.staff_settings import demo_defaults
from services.weekly_roster import generate_weekly_roster
from services.whatsapp_provider import FakeWhatsAppProvider
from services.whatsapp_store import PrivateFileStore, WorkflowStore
from services.whatsapp_workflow import ForecastWorker

TOKEN_A = "synthetic-test-only-hotel-a-access-token"
TOKEN_B = "synthetic-test-only-hotel-b-access-token"


@pytest.fixture
def system(tmp_path):
    store = WorkflowStore(tmp_path / "shared-worker.sqlite3")
    files = PrivateFileStore(tmp_path / "private")
    for tenant, manager, code in (("hotel-a", "gm-a", DEMO_CODE), ("hotel-b", "gm-b", "OTHER")):
        store.enroll_tenant(tenant_id=tenant, manager_id=manager, hotel_code=code, hotel_name=f"Fictional {tenant}")
        payload = demo_defaults()
        payload["staff"][0]["display_name"] = f"Synthetic employee {tenant}"
        store.save_staff_settings(manager, payload, dataset_mode="demo")
        store.save_hotel_parameters(manager, {"room_capacity": 400 if tenant == "hotel-a" else 292})
    store.enroll_sender(tenant_id="hotel-a", phone_number_id=DEMO_PHONE_ID, sender=DEMO_SENDER)
    auth = TokenMapAuthenticator({TOKEN_A: HotelPrincipal("hotel-a", "gm-a"), TOKEN_B: HotelPrincipal("hotel-b", "gm-b")})
    app = create_private_app(store, files, auth)
    return store, files, app, TestClient(app)


def headers(token=TOKEN_A, **extra):
    return {"Authorization": f"Bearer {token}", **extra}


def seed_roster(store, manager="gm-a", start=date(2026, 10, 23)):
    rows = empty_forecast(start)
    for row in rows:
        row.update(departures=12, stayovers=40, arrivals=20, rooms_on_books=60,
                   forecast_pickup=0, forecast_occupied=60, occupancy_pct=15)
    roster = generate_weekly_roster(rows, store.get_staff_settings(manager))
    roster["source"] = {"channel": "whatsapp", "hotel_name": "Fictional " + manager,
                        "source_key": "private/secret-upload.pdf", "job_id": "private-job-id", "sender": "15555550999"}
    roster["source_key"] = "another-private-path"
    return store.save_weekly_roster(manager, roster["week_start"], roster)


def test_factory_uses_injected_store_and_default_auth_denies_without_creating_data(system):
    store, files, app, client = system
    assert app.state.workflow_store is store and app.state.private_files is files
    denied = TestClient(create_private_app(store, files))
    assert denied.get("/private/api/settings", headers=headers()).status_code == 401
    shell = denied.get("/private")
    assert shell.status_code == 200 and "Open your workspace" in shell.text
    assert "Fictional hotel-a" not in shell.text and TOKEN_A not in shell.text
    assert denied.get("/openapi.json").status_code == 404
    assert denied.get("/docs").status_code == 404
    assert denied.get("/private/assets/secret.sqlite3").status_code == 404
    assert shell.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in shell.headers["content-security-policy"]


@pytest.mark.parametrize("method,path", [("GET", "/settings"), ("GET", "/parameters"), ("PATCH", "/parameters"),
    ("GET", "/staff"), ("PATCH", "/staff"), ("GET", "/rosters"), ("GET", "/rosters/2026-10-23"),
    ("GET", "/rosters/2026-10-23/versions/1"), ("GET", "/rosters/2026-10-23/versions/1/pdf")])
def test_every_private_route_requires_bearer_before_body_parsing(system, method, path):
    client = system[3]
    response = client.request(method, "/private/api" + path, content=b"not-json", headers={"Content-Type": "application/json"})
    assert response.status_code == 401
    assert TOKEN_A not in response.text
    assert client.request(method, "/private/api" + path, headers=headers("invalid")).status_code == 401


def test_query_cookie_duplicate_headers_and_wrong_mapping_cannot_choose_tenant(system):
    store, files, _, client = system
    assert client.get(f"/private/api/settings?token={TOKEN_A}").status_code == 401
    assert client.get("/private/api/settings", cookies={"token": TOKEN_A}).status_code == 401
    assert client.get("/private/api/settings", headers=[("Authorization", f"Bearer {TOKEN_A}"), ("Authorization", f"Bearer {TOKEN_B}")]).status_code == 401
    response = client.get("/private/api/settings?tenant_id=hotel-b&manager_id=gm-b", headers=headers()).json()
    assert response["hotel_name"] == "Fictional hotel-a"
    assert response["staff"][0]["display_name"] == "Synthetic employee hotel-a"
    mismatched = TokenMapAuthenticator({TOKEN_A: HotelPrincipal("hotel-a", "gm-b")})
    assert TestClient(create_private_app(store, files, mismatched)).get("/private/api/settings", headers=headers()).status_code == 403


def test_revoked_tenant_and_missing_settings_fail_closed_without_seed(system):
    store, _, _, client = system
    with store._connect() as db:
        db.execute("UPDATE wa_tenants SET enabled=0 WHERE tenant_id='hotel-a'")
    assert client.get("/private/api/settings", headers=headers()).status_code == 403
    with store._connect() as db:
        db.execute("UPDATE wa_tenants SET enabled=1 WHERE tenant_id='hotel-a'")
        db.execute("DELETE FROM staff_settings WHERE manager_id='gm-a'")
    assert client.get("/private/api/settings", headers=headers()).status_code == 404
    assert store.get_staff_settings("gm-a") is None


def test_parameter_edit_uses_canonical_settings_and_revision_preconditions(system):
    store, _, _, client = system
    before_b = store.get_staff_settings("gm-b")
    response = client.get("/private/api/parameters", headers=headers())
    revision = response.json()["revision"]
    assert response.headers["etag"] == f'"{revision}"'
    assert client.patch("/private/api/parameters", headers=headers(), json={"values": {"room_capacity": 292}}).status_code == 428
    assert client.patch("/private/api/parameters", headers=headers(**{"If-Match": "*"}), json={"values": {"room_capacity": 292}}).status_code == 400
    assert client.patch("/private/api/parameters", headers=headers(**{"If-Match": f'"{revision+1}"'}), json={"values": {"room_capacity": 292}, "expected_revision": revision}).status_code == 400
    saved = client.patch("/private/api/parameters", headers=headers(**{"If-Match": f'"{revision}"'}), json={"values": {"room_capacity": 292, "checkout_minutes": 25}})
    assert saved.status_code == 200
    assert saved.json()["revision"] == revision + 1
    canonical = store.get_staff_settings("gm-a")
    assert saved_room_capacity(canonical) == 292
    assert canonical["settings"]["cleaning_standards"]["checkout_minutes"] == 25
    assert store.get_staff_settings("gm-b") == before_b
    stale = client.patch("/private/api/parameters", headers=headers(), json={"values": {"room_capacity": 300}, "expected_revision": revision})
    assert stale.status_code == 409 and saved_room_capacity(store.get_staff_settings("gm-a")) == 292


@pytest.mark.parametrize("body", [
    {"values": {"room_capacity": True}, "expected_revision": 2},
    {"values": {"room_capacity": 0}, "expected_revision": 2},
    {"values": {"room_capacity": 292.5}, "expected_revision": 2},
    {"values": {"checkout_minutes": -1}, "expected_revision": 2},
    {"values": {"manager_id": "gm-b"}, "expected_revision": 2},
    {"values": {"room_capacity": 292}, "expected_revision": 2, "tenant_id": "hotel-b"},
    {"values": {"room_capacity": 292}, "expected_revision": True},
])
def test_invalid_parameter_body_never_changes_any_record(system, body):
    store, _, _, client = system
    before = store.get_staff_settings("gm-a")
    assert client.patch("/private/api/parameters", headers=headers(), json=body).status_code == 422
    assert store.get_staff_settings("gm-a") == before


def test_staff_edit_preserves_settings_and_rejects_stale_or_invalid_rows(system):
    store, _, _, client = system
    current = client.get("/private/api/settings", headers=headers()).json()
    staff = deepcopy(current["staff"])
    staff[0]["requested_time_off"] = "2026-10-23"
    staff[0]["display_name"] = "<img src=x onerror=alert(1)> Synthetic only"
    saved = client.patch("/private/api/staff", headers=headers(), json={"staff": staff, "expected_revision": current["revision"]})
    assert saved.status_code == 200
    assert saved.json()["settings"] == current["settings"]
    assert store.get_staff_settings("gm-a")["staff"] == staff
    assert store.get_staff_settings("gm-b")["staff"][0]["requested_time_off"] == ""
    assert client.patch("/private/api/staff", headers=headers(), json={"staff": current["staff"], "expected_revision": current["revision"]}).status_code == 409
    staff[1]["staff_id"] = staff[0]["staff_id"]
    assert client.patch("/private/api/staff", headers=headers(), json={"staff": staff, "expected_revision": saved.json()["revision"]}).status_code == 422
    assert client.patch("/private/api/staff", headers=headers(), json={"staff": [], "expected_revision": saved.json()["revision"]}).status_code == 422


def test_parameter_cas_catches_an_intervening_write(system, monkeypatch):
    store, _, _, client = system
    original = store.save_hotel_parameters
    def race(manager_id, values, expected_revision=None):
        original(manager_id, {"stayover_minutes": 12})
        return original(manager_id, values, expected_revision=expected_revision)
    monkeypatch.setattr(store, "save_hotel_parameters", race)
    result = client.patch("/private/api/parameters", headers=headers(), json={"values": {"room_capacity": 292}, "expected_revision": 2})
    assert result.status_code == 409
    saved = store.get_staff_settings("gm-a")
    assert saved_room_capacity(saved) == 400 and saved["settings"]["cleaning_standards"]["stayover_minutes"] == 12


def test_private_edits_and_worker_share_exact_latest_values_and_versions(system):
    store, files, app, client = system
    saved = client.patch("/private/api/parameters", headers=headers(), json={"values": {"checkout_minutes": 25}, "expected_revision": 2}).json()
    staff = deepcopy(saved["staff"])
    staff[0]["requested_time_off"] = "2026-10-23"
    updated = client.patch("/private/api/staff", headers=headers(), json={"staff": staff, "expected_revision": saved["revision"]})
    assert updated.status_code == 200
    source = forecast_pdf()
    provider = FakeWhatsAppProvider(media={"synthetic-media-1": source})
    worker = ForecastWorker(store, files, provider, clock=lambda: DEMO_NOW.timestamp())
    assert worker.store is app.state.workflow_store
    store.ingest(webhook_payload(source), DEMO_NOW.timestamp())
    assert worker.process_one()
    roster = store.get_weekly_roster("gm-a", "2026-10-23")
    assert roster is not None
    canonical = store.get_staff_settings("gm-a")
    assert roster["input_snapshot"]["settings"] == canonical["settings"]
    assert roster["input_snapshot"]["staff"] == canonical["staff"]
    assert roster["input_snapshot"]["settings"]["cleaning_standards"]["checkout_minutes"] == 25
    versions = client.get("/private/api/rosters", headers=headers()).json()
    assert len(versions["versions"]) == 1 and versions["latest"][0]["version"] == 1
    assert not provider.sent  # No actual or fake outbound dispatch needed to view the app.


def test_versions_pdf_and_paths_are_tenant_bound_and_history_is_immutable(system):
    store, _, _, client = system
    old = seed_roster(store)
    new = deepcopy(old)
    new["schedule"][0]["locked"] = True
    new["schedule"][0]["notes"] = "New synthetic lock"
    store.save_weekly_roster("gm-a", old["week_start"], new)
    seed_roster(store, manager="gm-b", start=date(2026, 10, 30))
    listed = client.get("/private/api/rosters", headers=headers()).json()
    assert [row["version"] for row in listed["versions"]] == [2, 1]
    assert listed["latest"][0]["version"] == 2
    historical = client.get("/private/api/rosters/2026-10-23/versions/1", headers=headers())
    latest = client.get("/private/api/rosters/2026-10-23", headers=headers()).json()
    assert historical.status_code == 200 and historical.json()["schedule"][0]["locked"] is False
    assert latest["schedule"][0]["locked"] is True
    assert "private/" not in historical.text and "private-job-id" not in historical.text and "15555550999" not in historical.text
    assert "source_key" not in historical.text and "input_snapshot" not in historical.text
    for path in ("/rosters/2026-10-23", "/rosters/2026-10-23/versions/1", "/rosters/2026-10-23/versions/1/pdf"):
        assert client.get("/private/api" + path, headers=headers(TOKEN_B)).status_code == 404
    pdf = client.get("/private/api/rosters/2026-10-23/versions/1/pdf", headers=headers())
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF-")
    assert pdf.headers["cache-control"] == "no-store" and "attachment" in pdf.headers["content-disposition"]
    text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(pdf.content)).pages)
    assert "Synthetic employee hotel-a" in text and "hotel-b" not in text
    assert "Day-by-day calculation summary" in text
    assert "private/secret-upload.pdf" not in text


def test_body_limit_and_no_raw_private_file_route(system):
    _, _, _, client = system
    result = client.patch("/private/api/parameters", headers=headers(**{"Content-Type": "application/json"}), content=(b"x" * (1024 * 1024) for _ in range(3)))
    assert result.status_code == 413
    assert client.get("/private/api/files/private/secret-upload.pdf", headers=headers()).status_code == 404


def test_static_client_uses_memory_only_and_safe_dom_operations(system):
    client = system[3]
    javascript = client.get("/private/assets/app.js").text
    for forbidden in ("localStorage", "sessionStorage", "innerHTML", "document.cookie", "console.log"):
        assert forbidden not in javascript
    assert "textContent" in javascript and "credentials: \"omit\"" in javascript
    assert "Authorization: \"Bearer \" + session.token" in javascript
    assert "expected_revision" in javascript
    assert "URL.revokeObjectURL" in javascript


def test_server_auth_configuration_is_explicit_and_errors_do_not_echo_tokens(monkeypatch, caplog):
    monkeypatch.delenv("PRIVATE_HOTEL_TOKENS_JSON", raising=False)
    assert TokenMapAuthenticator.from_environment()(TOKEN_A) is None
    monkeypatch.setenv("PRIVATE_HOTEL_TOKENS_JSON", json.dumps([{"token": TOKEN_A, "tenant_id": "hotel-a", "manager_id": "gm-a"}]))
    assert TokenMapAuthenticator.from_environment()(TOKEN_A) == HotelPrincipal("hotel-a", "gm-a")
    monkeypatch.setenv("PRIVATE_HOTEL_TOKENS_JSON", json.dumps([{"token": TOKEN_A, "tenant_id": "hotel-a", "manager_id": "gm-a"}] * 2))
    with pytest.raises(ValueError) as error:
        TokenMapAuthenticator.from_environment()
    assert TOKEN_A not in str(error.value) and TOKEN_A not in caplog.text


def test_scope_notes_do_not_claim_unsupported_weekly_calculations(system):
    response = system[3].get("/private/api/settings", headers=headers()).json()
    scope = response["calculation_scope"]
    assert scope["inspection_buffer_in_weekly_demand"] is False
    assert scope["stayover_policy_text_filters_counts"] is False
    assert "all stayovers" in scope["weekly_cleaning_demand"]


def test_client_session_races_are_aborted_and_never_mix_tenant_data():
    """Hermetic Node VM: mocked fetch ignores abort so epoch checks must work too."""
    import shutil
    import subprocess
    from pathlib import Path
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for deterministic browser-JavaScript race tests")
    javascript = Path(__file__).resolve().parents[1] / "api" / "private_app_static" / "app.js"
    program = r'''
const assert=require('assert'),fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1],'utf8');
class Element {
 constructor(id=''){this.id=id;this.value='';this.textContent='';this.hidden=false;this.children=[];this.events={};}
 addEventListener(name,fn){this.events[name]=fn;}
 append(...items){this.children.push(...items);}
 replaceChildren(...items){this.children=items;this.textContent='';}
 fire(name){return this.events[name]({preventDefault(){}});}
 click(){return this.events.click ? this.fire('click') : undefined;}
}
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
const settings=(who)=>({hotel_name:who,revision:1,parameters:{room_capacity:292,checkout_minutes:22,stayover_minutes:10},staff:[{display_name:who}],settings:{}});
const roster=(who)=>({week_start:'2026-10-23',week_end:'2026-10-29',version:1,feasible:true,calculations:[],constraints:[],schedule:[{display_name:who,date:'2026-10-23',code:'SHIFT',start:'09:00',end:'17:00',locked:false}]});
const versions={versions:[{week_start:'2026-10-23',version:1,feasible:true}]};
const response=(data,status=200)=>({ok:status>=200&&status<300,status,json:async()=>data,blob:async()=>data});
async function flush(){for(let i=0;i<12;i++)await Promise.resolve();}
function harness(){
 const elements=new Map(),requests=[];const get=(id)=>{if(!elements.has(id))elements.set(id,new Element(id));return elements.get(id);};
 get('workspace').hidden=true;get('roster-detail').hidden=true;
 const sandbox={document:{getElementById:get,createElement:()=>new Element()},window:{confirm:()=>true},AbortController,
 URL:{createObjectURL:()=>{throw new Error('Unexpected stale download');},revokeObjectURL(){}},setTimeout,
 fetch:(url,options)=>{const task=deferred();requests.push({url,options,...task});return task.promise;}};
 vm.runInNewContext(source,sandbox);
 return {get,requests};
}
function startLogin(h,who){h.get('access-token').value='token-'+who;return h.get('login-form').fire('submit');}
async function finishLogin(h,pending,who,index){h.requests[index].resolve(response(settings(who)));await flush();h.requests[index+1].resolve(response(versions));await pending;}
async function logged(who='A'){const h=harness();const p=startLogin(h,who);await finishLogin(h,p,who,0);return h;}
function assertTenant(h,who){assert.equal(h.get('hotel-name').textContent,who);assert.equal(JSON.parse(h.get('staff-json').value)[0].display_name,who);assert.equal(h.get('workspace').hidden,false);}
function assertCleared(h){assert.equal(h.get('workspace').hidden,true);assert.equal(h.get('staff-json').value,'');assert.equal(h.get('hotel-name').textContent,'');assert.equal(h.get('roster-detail').hidden,true);assert.equal(h.get('schedule').children.length,0);}
(async()=>{
 // A late successful login must never replace B or copy A staff with B's token.
 let h=harness();let a=startLogin(h,'A');let b=startLogin(h,'B');assert(h.requests[0].options.signal.aborted);
 await finishLogin(h,b,'B',1);assertTenant(h,'B');h.requests[0].resolve(response(settings('A')));await a;assertTenant(h,'B');assert.equal(h.requests.length,3);
 let save=h.get('staff-form').fire('submit');assert.equal(h.requests[3].options.headers.Authorization,'Bearer token-B');assert.equal(JSON.parse(h.requests[3].options.body).staff[0].display_name,'B');h.requests[3].resolve(response(settings('B')));await save;
 // A body decode that completes after switching tokens is stale too.
 h=harness();a=startLogin(h,'A');const body=deferred();h.requests[0].resolve({...response({}),json:()=>body.promise});await flush();b=startLogin(h,'B');await finishLogin(h,b,'B',1);body.resolve(settings('A'));await a;assertTenant(h,'B');
 // Stale errors cannot clear B's session or replace its success message.
 h=harness();a=startLogin(h,'A');b=startLogin(h,'B');await finishLogin(h,b,'B',1);h.requests[0].resolve(response({},401));await a;assertTenant(h,'B');assert(h.get('message').textContent.startsWith('Connected'));
 // A failed newer login must not allow an older successful login to unhide data.
 h=harness();a=startLogin(h,'A');b=startLogin(h,'B');h.requests[1].resolve(response({},401));await b;h.requests[0].resolve(response(settings('A')));await a;assertCleared(h);
 // Logout during each fetch/decode path aborts outstanding work and leaves no data.
 for(const action of ['view','parameters','staff','reload','reload-versions','pdf']){
   h=await logged();let operation;
   if(action==='view'){operation=h.get('versions').children[0].children[1].fire('click');}
   else if(action==='parameters'){operation=h.get('parameters-form').fire('submit');}
   else if(action==='staff'){operation=h.get('staff-form').fire('submit');}
   else if(action==='pdf'){
     const view=h.get('versions').children[0].children[1].fire('click');h.requests[2].resolve(response(roster('A')));await view;operation=h.get('download-pdf').fire('click');
   } else {operation=h.get('reload').fire('click');if(action==='reload-versions'){h.requests[2].resolve(response(settings('A')));await flush();}}
   const waiting=h.requests.at(-1);h.get('logout').fire('click');assert(waiting.options.signal.aborted);assertCleared(h);
   waiting.resolve(response(action==='view'?roster('A'):action==='reload-versions'?versions:settings('A')));await operation;assertCleared(h);assert(h.get('message').textContent.startsWith('Signed out'));
 }
 console.log('Session race checks passed: delayed login, delayed body, stale errors, failed replacement login, logout during view/save/reload/PDF.');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run([node, "-e", program, str(javascript)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Session race checks passed" in result.stdout


def test_staff_compare_and_swap_preserves_an_intervening_parameter_write(system, monkeypatch):
    store, _, _, client = system
    current = store.get_staff_settings("gm-a")
    staff = deepcopy(current["staff"])
    staff[0]["requested_time_off"] = "2026-10-23"
    original = store.save_staff_settings
    def race(manager_id, payload, demo_seed_version=0, dataset_mode="operational", expected_revision=None):
        store.save_hotel_parameters(manager_id, {"room_capacity": 292})
        return original(manager_id, payload, demo_seed_version, dataset_mode, expected_revision)
    monkeypatch.setattr(store, "save_staff_settings", race)
    response = client.patch("/private/api/staff", headers=headers(), json={"staff": staff, "expected_revision": current["_settings_revision"]})
    assert response.status_code == 409
    saved = store.get_staff_settings("gm-a")
    assert saved_room_capacity(saved) == 292
    assert saved["staff"][0]["requested_time_off"] == ""


def test_duplicate_server_auth_configuration_keys_are_rejected(monkeypatch):
    monkeypatch.setenv("PRIVATE_HOTEL_TOKENS_JSON", '[{"token":"' + TOKEN_A + '","tenant_id":"hotel-a","tenant_id":"hotel-b","manager_id":"gm-a"}]')
    with pytest.raises(ValueError, match="Invalid private authentication configuration"):
        TokenMapAuthenticator.from_environment()


def test_expiring_owner_key_has_injected_maximum_ten_day_window():
    from api.private_hotel_app import ExpiringTokenMapAuthenticator
    now = [1000.0]
    principal = HotelPrincipal("hotel-a", "gm-a")
    auth = ExpiringTokenMapAuthenticator({TOKEN_A: principal}, issued_at=1000, expires_at=1000 + 240 * 3600, clock=lambda: now[0])
    assert auth(TOKEN_A) == principal
    now[0] = 999
    assert auth(TOKEN_A) is None
    now[0] = auth.expires_at - 0.001
    assert auth(TOKEN_A) == principal
    now[0] = auth.expires_at
    assert auth(TOKEN_A) is None
    for start, end in [(1, 1), (1, 240 * 3600 + 2), (float("nan"), 100), (True, 5)]:
        with pytest.raises(ValueError):
            ExpiringTokenMapAuthenticator({TOKEN_A: principal}, issued_at=start, expires_at=end)


def upload_app(system):
    from services.private_forecast_workflow import PrivateForecastWorkflow
    store, files, _, _ = system
    calls = []
    def evaluator(*args, **kwargs):
        from services.whatsapp_forecast import validate_whatsapp_forecast
        calls.append(True)
        return {"method": "native_pdf", "automatic_roster_allowed": True, "requires_review": False,
                "rows": validate_whatsapp_forecast(forecast_pdf(), "application/pdf", DEMO_CODE, 400, DEMO_NOW)}
    service = PrivateForecastWorkflow(store, files, clock=lambda: DEMO_NOW.timestamp(),
        admission_check=lambda tenant, manager, now: tenant == "hotel-a" and manager == "gm-a", media_evaluator=evaluator)
    auth = TokenMapAuthenticator({TOKEN_A: HotelPrincipal("hotel-a", "gm-a"), TOKEN_B: HotelPrincipal("hotel-b", "gm-b")})
    app = create_private_app(store, files, auth, forecast_workflow=service)
    return TestClient(app), service, calls, app


def test_optional_upload_disabled_and_workflow_must_share_exact_repository(system, tmp_path):
    from services.private_forecast_workflow import PrivateForecastWorkflow
    store, files, _, client = system
    assert client.get("/private/api/settings", headers=headers()).json()["forecast_uploads_available"] is False
    for path in ["/private/api/forecast-uploads", "/private/api/forecast-uploads/anything/confirm"]:
        response = client.post(path, headers=headers(), content=b"not-a-file")
        assert response.status_code == 503
    other = PrivateForecastWorkflow(WorkflowStore(tmp_path / "other.sqlite3"), files)
    with pytest.raises(ValueError, match="same injected"):
        create_private_app(store, files, forecast_workflow=other)


def test_private_upload_http_enqueues_reviews_and_exposes_new_version_without_whatsapp(system):
    store = system[0]
    client, service, calls, _ = upload_app(system)
    assert client.get("/private/api/settings", headers=headers()).json()["forecast_uploads_available"] is True
    args = {"content": forecast_pdf(), "headers": headers(**{"Content-Type": "application/pdf", "Idempotency-Key": "synthetic-api-upload-1"})}
    result = client.post("/private/api/forecast-uploads?tenant_id=hotel-b", **args)
    assert result.status_code == 202 and not calls
    item = result.json()
    assert item["status"] == "queued"
    assert client.post("/private/api/forecast-uploads", **args).json()["id"] == item["id"]
    assert not client.get("/private/api/rosters", headers=headers()).json()["versions"]
    assert service.process_one()
    item = client.get("/private/api/forecast-uploads/" + item["id"], headers=headers()).json()
    assert item["status"] == "needs_review" and len(item["rows"]) == 7
    path = "/private/api/forecast-uploads/" + item["id"] + "/confirm"
    result = client.post(path, headers=headers(), json={"expected_revision": item["revision"]})
    assert result.status_code == 200 and result.json()["status"] == "confirmed"
    assert client.post(path, headers=headers(), json={"expected_revision": item["revision"]}).status_code == 409
    assert not client.get("/private/api/rosters", headers=headers()).json()["versions"]
    assert service.process_one()
    versions = client.get("/private/api/rosters", headers=headers()).json()["versions"]
    assert len(versions) == 1 and versions[0]["source"]["channel"] == "private_app"
    pdf = client.get(f"/private/api/rosters/{versions[0]['week_start']}/versions/1/pdf", headers=headers())
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM wa_jobs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM wa_outbox").fetchone()[0] == 0
    view = client.get("/private/api/forecast-uploads", headers=headers()).json()
    assert "source_key" not in str(view) and "sha256" not in str(view)


def test_upload_routes_auth_scope_body_limit_and_invalid_actions(system):
    client, service, _, _ = upload_app(system)
    paths = ["/private/api/forecast-uploads", "/private/api/forecast-uploads/unknown/confirm"]
    for path in paths:
        assert client.post(path, content=b"secret-file").status_code == 401
        assert client.post(path, headers=headers(TOKEN_B), content=b"secret-file").status_code == 403
    assert client.post(paths[0], headers=headers(**{"Content-Type": "text/plain"}), content=b"text").status_code == 415
    assert client.post(paths[0], headers=headers(**{"Content-Type": "image/png"}), content=b"x" * (5 * 1024 * 1024 + 1)).status_code == 413
    assert client.post(paths[0], headers=headers(**{"Content-Type": "application/pdf"}), content=b"%PDF").status_code == 422
    item = client.post(paths[0], headers=headers(**{"Content-Type": "application/pdf", "Idempotency-Key": "synthetic-api-key"}), content=forecast_pdf()).json()
    assert client.get(paths[0] + "/" + item["id"], headers=headers(TOKEN_B)).status_code == 403
    assert client.post(paths[0] + "/" + item["id"] + "/confirm", headers=headers(), json={"expected_revision": item["revision"], "tenant_id": "hotel-b"}).status_code == 422
    assert client.post(paths[0] + "/" + item["id"] + "/cancel", headers=headers(), json={"expected_revision": item["revision"]}).status_code == 200
    assert not service.process_one()


def test_disabled_and_expired_upload_auth_do_not_consume_body(system):
    import asyncio
    from api.private_hotel_app import ExpiringTokenMapAuthenticator
    store, files, app, _ = system
    async def run(target):
        messages = []
        async def receive():
            raise AssertionError("Disabled or unauthorized upload consumed the source")
        async def send(message): messages.append(message)
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "method": "POST", "scheme": "https",
                 "path": "/private/api/forecast-uploads", "raw_path": b"/private/api/forecast-uploads", "query_string": b"", "root_path": "",
                 "headers": [(b"authorization", ("Bearer " + TOKEN_A).encode()), (b"content-type", b"application/pdf")],
                 "client": ("127.0.0.1", 12345), "server": ("test", 443), "http_version": "1.1"}
        await target(scope, receive, send)
        return next(message["status"] for message in messages if message["type"] == "http.response.start")
    assert asyncio.run(run(app)) == 503
    expired = ExpiringTokenMapAuthenticator({TOKEN_A: HotelPrincipal("hotel-a", "gm-a")}, issued_at=100, expires_at=200, clock=lambda: 200)
    assert asyncio.run(run(create_private_app(store, files, expired))) == 401


def test_nonblocking_upload_intake_allows_one_body_and_releases_after_error(system):
    import asyncio
    import httpx
    _, service, _, app = upload_app(system)
    async def scenario():
        started, resume = asyncio.Event(), asyncio.Event()
        async def slow():
            started.set()
            await resume.wait()
            yield b"synthetic-file"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            kwargs = {"headers": headers(**{"Content-Type": "image/png", "Idempotency-Key": "synthetic-intake-1"})}
            first = asyncio.create_task(client.post("/private/api/forecast-uploads", content=slow(), **kwargs))
            await started.wait()
            second = await client.post("/private/api/forecast-uploads", content=b"synthetic-file", **kwargs)
            assert second.status_code == 429
            resume.set()
            assert (await first).status_code == 202
            bad = await client.post("/private/api/forecast-uploads", content=b"", **kwargs)
            assert bad.status_code == 422
            assert (await client.post("/private/api/forecast-uploads", content=b"synthetic-file", **kwargs)).status_code == 202
    asyncio.run(scenario())


def test_upload_javascript_repeated_cancel_back_and_cross_session_responses():
    import shutil
    import subprocess
    from pathlib import Path
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for deterministic upload-JavaScript tests")
    javascript = Path(__file__).resolve().parents[1] / "api" / "private_app_static" / "app.js"
    program = r'''
const assert=require('assert'),fs=require('fs'),vm=require('vm');
class Element {
 constructor(){this.value='';this.textContent='';this.hidden=false;this.children=[];this.events={};this.files=[];this.checked=false;this.disabled=false;}
 addEventListener(n,f){this.events[n]=f;} append(...items){this.children.push(...items);} replaceChildren(...items){this.children=items;this.textContent='';}
 fire(n){return this.events[n]({preventDefault(){}});} click(){return this.events.click?this.fire('click'):undefined;}
}
const defer=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
const response=(x,status=200)=>({ok:status<400,status,json:async()=>x});
const settings=(who)=>({hotel_name:who,revision:1,parameters:{room_capacity:292,checkout_minutes:22,stayover_minutes:10},staff:[{display_name:who}],forecast_uploads_available:true});
const item=(id,status='needs_review')=>({id,status,revision:3,created_at:123,review_expires_at:9999999999,settings_revision:1,message:id+' '+status,rows:[{date:'2026-10-23',departures:1,stayovers:2,arrivals:3,rooms_on_books:4,forecast_pickup:5,forecast_occupied:6,occupancy_pct:2}]});
async function flush(){for(let i=0;i<20;i++)await Promise.resolve();}
function harness(){const elements=new Map(),requests=[],events={};let uuid=0;const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
const window={confirm:()=>true,addEventListener:(n,f)=>{events[n]=f;}};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),{document:{getElementById:get,createElement:()=>new Element()},window,AbortController,crypto:{randomUUID:()=>`synthetic-key-${++uuid}`},setTimeout,fetch:(url,options)=>{const task=defer();requests.push({url,options,...task});return task.promise;}});
return {get,requests,events,window};}
async function login(h,who='A'){h.get('access-token').value='token-'+who;const index=h.requests.length,p=h.get('login-form').fire('submit');h.requests[index].resolve(response(settings(who)));await flush();h.requests[index+1].resolve(response({versions:[]}));await flush();h.requests[index+2].resolve(response({uploads:[item(who)]}));await p;}
async function view(h,index=0){const start=h.requests.length,p=h.get('uploads').children[index].children[1].fire('click');h.requests[start].resolve(response(item('A')));await p;}
function cleared(h){assert(h.get('workspace').hidden);assert(h.get('upload-detail').hidden);assert.equal(h.get('review-rows').children.length,0);assert.equal(h.get('forecast-file').value,'');}
(async()=>{
let h=harness();await login(h);h.get('forecast-file').files=[{type:'image/png',size:10}];
let p=h.get('upload-form').fire('submit');assert.equal(h.requests.length,4);await h.get('upload-form').fire('submit');assert.equal(h.requests.length,4);assert(h.get('forecast-file').disabled);
h.requests[3].resolve(response(item('upload','queued')));await flush();h.requests[4].resolve(response({uploads:[item('upload','queued')]}));await p;assert(!h.get('forecast-file').disabled);assert(!h.get('upload-detail').hidden);
// A delayed upload cannot show its report or clear B's selected file after a new login.
h=harness();await login(h);h.get('forecast-file').files=[{type:'application/pdf',size:100}];p=h.get('upload-form').fire('submit');const old=h.requests[3];await login(h,'B');h.get('forecast-file').value='B draft';old.resolve(response(item('A','queued')));await p;assert.equal(h.get('hotel-name').textContent,'B');assert.equal(h.get('forecast-file').value,'B draft');assert(h.get('upload-detail').hidden);assert(old.options.signal.aborted);
// Close/back while a detail fetch is pending must not reopen that review.
h=harness();await login(h);p=h.get('uploads').children[0].children[1].fire('click');h.get('close-upload').fire('click');h.requests[3].resolve(response(item('A')));await p;assert(h.get('upload-detail').hidden);
// A newly requested detail immediately invalidates the previous actionable review.
h=harness();await login(h);await view(h);h.get('review-checked').checked=true;
p=h.get('uploads').children[0].children[1].fire('click');const pendingIndex=h.requests.length-1;assert(h.get('upload-detail').hidden);await h.get('confirm-upload').fire('click');assert.equal(h.requests.length,pendingIndex+1);h.requests[pendingIndex].resolve(response(item('new-detail')));await p;assert.equal(h.get('upload-status').textContent,'new-detail needs_review');
// A previously submitted action cannot overwrite a newer loaded detail.
h=harness();await login(h);await view(h);h.get('review-checked').checked=true;const oldAction=h.get('confirm-upload').fire('click'),oldActionIndex=h.requests.length-1;
p=h.get('uploads').children[0].children[1].fire('click');h.requests.at(-1).resolve(response(item('newer-detail')));await p;h.requests[oldActionIndex].resolve(response(item('A','confirmed')));await flush();h.requests.at(-1).resolve(response({uploads:[item('newer-detail')]}));await oldAction;assert.equal(h.get('upload-status').textContent,'newer-detail needs_review');
// Confirm requires explicit checkbox and repeated actions create one request.
h=harness();await login(h);await view(h);const before=h.requests.length;await h.get('confirm-upload').fire('click');assert.equal(h.requests.length,before);
h.get('review-checked').checked=true;h.get('review-checked').fire('change');p=h.get('confirm-upload').fire('click');await h.get('confirm-upload').fire('click');assert.equal(h.requests.length,before+1);assert.equal(JSON.parse(h.requests.at(-1).options.body).expected_revision,3);
h.requests[before].resolve(response(item('A','confirmed')));await flush();h.requests[before+1].resolve(response({uploads:[item('A','confirmed')]}));await p;assert(h.get('confirm-upload').hidden);
// Declining cancel sends nothing; accepted cancel and regeneration remain session-scoped.
for(const action of ['cancel','regenerate','confirm']){h=harness();await login(h);await view(h);h.get('review-checked').checked=true;
if(action==='cancel'){h.window.confirm=()=>false;await h.get('cancel-upload').fire('click');assert.equal(h.requests.length,4);h.window.confirm=()=>true;}
p=h.get(action+'-upload').fire('click');const waiting=h.requests.at(-1);h.get('logout').fire('click');cleared(h);waiting.resolve(response(item('A',action==='cancel'?'cancelled':'queued')));await p;cleared(h);assert(waiting.options.signal.aborted);}
// bfcache Back/Forward cannot preserve an authenticated workspace.
h=harness();await login(h);await view(h);h.events.pagehide();cleared(h);
console.log('Upload UI race checks passed');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run([node, "-e", program, str(javascript)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Upload UI race checks passed" in result.stdout


def test_slow_upload_timeout_releases_intake(system, monkeypatch):
    import asyncio
    import httpx
    import api.private_hotel_app as module
    _, _, _, app = upload_app(system)
    monkeypatch.setattr(module, "BODY_TIMEOUT_SECONDS", 0.01)
    async def scenario():
        async def slow():
            await asyncio.sleep(1)
            yield b"synthetic-file"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            args = {"headers": headers(**{"Content-Type": "image/png", "Idempotency-Key": "synthetic-timeout"})}
            assert (await client.post("/private/api/forecast-uploads", content=slow(), **args)).status_code == 408
            assert (await client.post("/private/api/forecast-uploads", content=b"synthetic-file", **args)).status_code == 202
    asyncio.run(scenario())


def test_shared_storage_quota_returns_operator_only_507_without_enqueuing(system, tmp_path):
    from services.private_storage_budget import QuotaPrivateFileStore
    from services.private_forecast_workflow import PrivateForecastWorkflow
    store = system[0]
    files = QuotaPrivateFileStore(tmp_path / "bounded-private", max_bytes=3, free_reserve_bytes=0)
    files.put("hotel-b", b"old", "pdf")  # Shared storage, not just this manager's uploads.
    service = PrivateForecastWorkflow(store, files, clock=lambda: DEMO_NOW.timestamp(), admission_check=lambda *args: True)
    auth = TokenMapAuthenticator({TOKEN_A: HotelPrincipal("hotel-a", "gm-a")})
    client = TestClient(create_private_app(store, files, auth, forecast_workflow=service))
    response = client.post("/private/api/forecast-uploads", content=b"new-file", headers=headers(**{"Content-Type": "image/png", "Idempotency-Key": "synthetic-quota-upload"}))
    assert response.status_code == 507
    assert "Contact the operator" in response.json()["detail"] and "do not repeatedly resend" in response.json()["detail"]
    assert not service.list_uploads("hotel-a", "gm-a")
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM wa_outbox").fetchone()[0] == 0
