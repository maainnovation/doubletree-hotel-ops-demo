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
