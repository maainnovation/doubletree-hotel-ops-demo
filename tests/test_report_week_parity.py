"""Synthetic historical/current/future report parity; no external transport.

Extraction assessments are injected, while both channels run real validation,
review, saved settings, scheduling and version persistence. Parser-specific
layout tests belong to the media-review suite.
"""
from copy import deepcopy
from datetime import date, timedelta
import re

import pytest

from test_shared_forecast_workflow import shared, signed, incoming, private_headers
from test_private_forecast_workflow import private_system, upload, confirm
from services.private_forecast_workflow import PrivateForecastWorkflow, PrivateForecastError
from services.twilio_pilot import synthetic_forecast
from services.whatsapp_forecast import validate_whatsapp_forecast
from scripts.demo_whatsapp_workflow import DEMO_NOW
from services.twilio_pilot import HOTEL_CODE

WEEKS = (date(2025, 10, 24), date(2026, 10, 23), date(2028, 10, 20))


def assessment(rows, *, review=False):
    return {'method': 'native_layout_review' if review else 'native_pdf',
            'automatic_roster_allowed': not review, 'requires_review': review,
            'candidate_valid_for_confirmation': True, 'rows': deepcopy(rows)}


def test_historical_current_future_shared_counts_settings_and_independent_versions(shared):
    s=shared
    s['store'].save_hotel_parameters('manager-a', {'checkout_minutes': 25})
    current={'rows':None}
    service=PrivateForecastWorkflow(s['store'],s['files'],clock=lambda:s['clock'][0],
        admission_check=lambda *args:True,
        media_evaluator=lambda *args,**kwargs:assessment(current['rows'],review=True))
    originals={}
    for index,week in enumerate(WEEKS):
        source=synthetic_forecast(week)
        rows=validate_whatsapp_forecast(source,'application/pdf',HOTEL_CODE,292,DEMO_NOW)
        current['rows']=rows
        s['worker'].media_evaluator=lambda *args,**kwargs:assessment(current['rows'])
        assert signed(s,incoming(s,'SM'+format(index+1,'032x'),data=source)).status_code==200
        s['worker'].drain_ready();s['worker'].drain_ready()
        wa=s['store'].get_weekly_roster('manager-a',week.isoformat())
        assert wa is not None and wa['version']==1
        assert wa['forecast']==rows and wa['week_start']==week.isoformat()
        assert wa['demand_minutes']=={row['date']:int(row['departures']*25+row['stayovers']*s['store'].get_staff_settings('manager-a')['settings']['cleaning_standards']['stayover_minutes']) for row in rows}
        originals[week.isoformat()]=deepcopy(wa)
        item=service.enqueue('hotel-a','manager-a',source,'application/pdf',f'parity-private-{index}')
        assert service.process_one()
        item=service.get('hotel-a','manager-a',item['id'])
        assert item['status']=='needs_review' and item['rows']==rows
        assert s['store'].get_weekly_roster('manager-a',week.isoformat())['version']==1
        service.action('hotel-a','manager-a',item['id'],'confirm',item['revision'])
        assert service.process_one()
        private=s['store'].get_weekly_roster('manager-a',week.isoformat())
        assert private['version']==2 and private['forecast']==wa['forecast']
        assert private['demand_minutes']==wa['demand_minutes'] and private['schedule']==wa['schedule']
        assert private['source']['settings_revision']==wa['source']['settings_revision']
        # Later weeks never replace or increment an earlier week's latest version.
        for known in originals:
            assert s['store'].get_weekly_roster('manager-a',known)['version']==2
    versions=s['store'].list_weekly_roster_versions('manager-a')
    assert {(r['week_start'],r['version']) for r in versions}=={(w.isoformat(),v) for w in WEEKS for v in (1,2)}
    for record in versions:
        if record['version']==1:
            assert record['forecast']==originals[record['week_start']]['forecast']
            assert record['schedule']==originals[record['week_start']]['schedule']
    visible=s['client'].get('/private/api/rosters',headers=private_headers())
    assert visible.status_code==200 and len(visible.json()['versions'])==6
    assert not s['store'].list_weekly_roster_versions('manager-b')


@pytest.mark.parametrize('week',WEEKS,ids=['historical','current','future'])
def test_layout_review_never_autogenerates_before_whatsapp_confirmation(shared,week):
    s=shared;source=synthetic_forecast(week)
    rows=validate_whatsapp_forecast(source,'application/pdf',HOTEL_CODE,292,DEMO_NOW)
    s['worker'].media_evaluator=lambda *args,**kwargs:assessment(rows,review=True)
    assert signed(s,incoming(s,'SM'+'d'*32,data=source)).status_code==200
    s['worker'].drain_ready()
    assert not s['store'].list_weekly_roster_versions('manager-a')
    prompt=s['provider'].sent[-1]
    assert prompt['type']=='text' and week.isoformat() in prompt['text']
    code=re.search(r'CONFIRM ([0-9a-f]{16})',prompt['text']).group(1)
    assert signed(s,incoming(s,'SM'+'e'*32,body='CONFIRM '+code)).status_code==200
    s['worker'].drain_ready()
    saved=s['store'].get_weekly_roster('manager-a',week.isoformat())
    assert saved is not None and saved['forecast']==rows and saved['version']==1
    assert s['provider'].sent[-1]['type']=='document'


@pytest.mark.parametrize('barrier',['inactive_approval','review_expiry'])
def test_historical_private_report_cannot_bypass_authorization_or_review_deadline(private_system,barrier):
    store,files,service,now,enabled,rows,calls=private_system
    for i,row in enumerate(rows):row['date']=(WEEKS[0]+timedelta(days=i)).isoformat()
    item=upload(service)
    assert service.process_one()
    item=service.get('hotel-a','gm-a',item['id'])
    assert item['status']=='needs_review' and item['rows'][0]['date']==WEEKS[0].isoformat()
    if barrier=='inactive_approval':enabled[0]=False
    else:now[0]+=1801
    with pytest.raises(PrivateForecastError):confirm(service,item)
    assert not store.list_weekly_roster_versions('gm-a')
    assert not store.rows('wa_outbox')


@pytest.mark.parametrize('barrier',['review_expiry','sender_revoked','outside_reply_window'])
def test_historical_whatsapp_report_does_not_extend_review_or_reply_window(shared,barrier):
    s=shared;source=synthetic_forecast(WEEKS[0])
    rows=validate_whatsapp_forecast(source,'application/pdf',HOTEL_CODE,292,DEMO_NOW)
    s['worker'].media_evaluator=lambda *args,**kwargs:assessment(rows,review=True)
    assert signed(s,incoming(s,'SM'+'a'*32,data=source,
        created=s['clock'][0]-86401 if barrier=='outside_reply_window' else None)).status_code==200
    s['worker'].drain_ready()
    if barrier!='outside_reply_window':
        code=re.search(r'CONFIRM ([0-9a-f]{16})',s['provider'].sent[-1]['text']).group(1)
        if barrier=='review_expiry':s['clock'][0]+=1801
        else:
            with s['store']._connect() as db:db.execute("UPDATE wa_senders SET enabled=0")
        assert signed(s,incoming(s,'SM'+'b'*32,body='CONFIRM '+code)).status_code==200
        s['worker'].drain_ready()
    assert not s['store'].list_weekly_roster_versions('manager-a')
    assert not any(item['type']=='document' for item in s['provider'].sent)
