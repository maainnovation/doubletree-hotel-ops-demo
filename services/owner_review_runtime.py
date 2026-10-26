"""Explicit owner-test composition. No default activation, keys or approval creation."""
from dataclasses import dataclass
from datetime import date,datetime,timezone
import os
from pathlib import Path
import re
import time

import httpx

from api.private_hotel_app import ExpiringTokenMapAuthenticator,HotelPrincipal
from services.private_forecast_workflow import PrivateForecastWorkflow
from services.forecast_media_worker import evaluate_forecast_media_isolated
from services.review_pilot_cost import ReviewPilotCostMonitor
from services.twilio_pilot import PilotConfig,PilotConfigurationError,TENANT,MANAGER
from services.twilio_review_pilot import build_review_pilot_runtime, _fixtures
from scripts.measure_review_runtime import CgroupMemoryGuard, resource_profile


_WARM_FIXTURES = None

def warm_owner_dependencies():
    """Include fixture/native-font startup in DISABLED service memory evidence.

    Only fixed fictional bytes are retained. No environment, database, account,
    source upload or network is accessed; never import the optional ONNX model.
    """
    global _WARM_FIXTURES
    if _WARM_FIXTURES is None:
        _WARM_FIXTURES = {profile:_fixtures(date(2026,10,23),profile)
                          for profile in ('canonical_v1','hotel_daily_table_v1')}


def legacy_config_from_environment(environment=None):
    """Read existing identity, including its UNCHANGED historical expiry/week."""
    env=os.environ if environment is None else environment
    try:
        expiry=datetime.fromisoformat(env['PILOT_APPROVED_UNTIL'].replace('Z','+00:00'))
        if expiry.tzinfo is None or env.get('PILOT_VOLUME_CONFIRMED')!='1':raise ValueError
        return PilotConfig(env['TWILIO_ACCOUNT_SID'],env['TWILIO_AUTH_TOKEN'],env['TWILIO_WHATSAPP_FROM'].removeprefix('whatsapp:+').removeprefix('+'),
            env['PILOT_MANAGER_PHONE'],env['PILOT_EXTERNAL_ORIGIN'].rstrip('/'),
            Path(env.get('PILOT_DATA_ROOT','/var/data/whatsapp-pilot')),expiry.timestamp(),date.fromisoformat(env['PILOT_WEEK_START']))
    except (KeyError,ValueError,TypeError,AttributeError):
        raise PilotConfigurationError('Existing owner identity and confirmed volume configuration are required.') from None


@dataclass
class OwnerComponents:
    runtime: object
    authenticator: object
    app_workflow: object
    cost_monitor: object
    media_evaluator: object
    memory_guard_factory: object
    whatsapp_enabled: bool = False


def build_owner_components(environment=None,*,clock=time.time,transport=None,memory_guard_factory=CgroupMemoryGuard):
    warm_owner_dependencies()
    env=os.environ if environment is None else environment
    if env.get('PILOT_REVIEW_ENABLED','0')!='1':return None
    if env.get('PILOT_ENABLED','0')!='0' or env.get('PILOT_REVIEW_RESOURCE_CONFIRMED')!='1':
        raise PilotConfigurationError('Disable legacy intake and verify the disabled-stage resource gate first.')
    key=env.get('PILOT_REVIEW_APP_KEY','')
    identifier=env.get('PILOT_REVIEW_APPROVAL_ID','')
    if (not isinstance(key,str) or not 32<=len(key)<=512 or not key.isascii() or any(ord(c)<=32 or ord(c)==127 for c in key)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',identifier)):
        raise PilotConfigurationError('An operator-approved owner epoch and privately entered unique app key are required.')
    if transport is None and memory_guard_factory is not CgroupMemoryGuard:
        raise PilotConfigurationError('Live review requires the actual cgroup resource guard.')
    try:
        profile, _, _ = resource_profile(env)
    except ValueError:
        raise PilotConfigurationError('An allowlisted owner review resource profile is required.') from None
    if memory_guard_factory is CgroupMemoryGuard:
        memory_guard_factory = lambda: CgroupMemoryGuard(profile=profile)
    config=legacy_config_from_environment(env)
    if key in {config.auth_token,config.account_sid}:
        raise PilotConfigurationError('A distinct private owner app key is required.')
    whatsapp_enabled=env.get("PILOT_REVIEW_WHATSAPP_ENABLED","0")=="1"
    holder={}
    def cost_gate(db,now):
        monitor=holder.get('monitor')
        return whatsapp_enabled and monitor is not None and monitor.cost_gate(db,now) is True
    runtime=build_review_pilot_runtime(config,identifier,enabled=True,transport=transport,clock=clock,cost_gate=cost_gate)
    monitor=ReviewPilotCostMonitor(runtime.store,config.account_sid,config.auth_token,identifier,clock=clock,transport=transport)
    holder['monitor']=monitor
    approval=runtime.assert_active()
    expiring_auth=ExpiringTokenMapAuthenticator({key:HotelPrincipal(TENANT,MANAGER)},issued_at=approval['approved_at'],expires_at=approval['expires_at'],clock=clock)
    def auth(token):
        principal=expiring_auth(token)
        if principal is None or runtime.owner_active_for_app(TENANT,MANAGER,clock()) is not True:
            return None
        return principal
    def evaluator(*args,**kwargs):
        return evaluate_forecast_media_isolated(*args,**kwargs,memory_guard=memory_guard_factory())
    app_workflow=PrivateForecastWorkflow(runtime.store,runtime.files,clock=clock,
        admission_check=runtime.owner_active_for_app,media_evaluator=evaluator)
    return OwnerComponents(runtime,auth,app_workflow,monitor,evaluator,memory_guard_factory,whatsapp_enabled)
