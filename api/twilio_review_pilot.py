"""Owner-only review pilot surface. Deployment alone does not activate intake."""
from collections import deque
import asyncio
import os
import re
import time
from urllib.parse import parse_qsl

from fastapi import Header,HTTPException,Request
from fastapi.responses import Response
from api.private_hotel_app import create_private_app
from services.owner_review_runtime import build_owner_components
from services.twilio_media import MediaGrantError
from services.twilio_provider import validate_twilio_signature
from services.whatsapp_provider import ProviderError

WEBHOOK_RECEIVE_SECONDS=15
ACK='<?xml version="1.0" encoding="UTF-8"?><Response/>'


def create_app(components=None,*,unavailable=False):
    rt=components.runtime if components else None
    app=create_private_app(rt.store if rt else None,rt.files if rt else None,
        components.authenticator if components else None,forecast_workflow=components.app_workflow if components else None)
    app.state.owner_components=components
    requests=deque()
    webhook_intake=asyncio.Lock()

    @app.middleware('http')
    async def limits(request,call_next):
        now=time.monotonic()
        while requests and requests[0]<now-60:requests.popleft()
        if request.url.path!='/health':
            if len(requests)>=120:return Response(status_code=429,headers={'Retry-After':'60','Cache-Control':'no-store'})
            requests.append(now)
        response=await call_next(request)
        response.headers['Cache-Control']='private, no-store, max-age=0'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['X-Content-Type-Options']='nosniff'
        return response

    def configured():
        if rt is None:raise HTTPException(503,'Owner test is disabled or unavailable')
        return rt

    def active():
        configured()
        try:rt.assert_active()
        except (ProviderError,ValueError):raise HTTPException(503,'Owner test is expired or unavailable') from None
        return rt

    async def signed_form(request,signature,url):
        if request.headers.get('content-type','').split(';',1)[0].lower()!='application/x-www-form-urlencoded':
            raise HTTPException(415,'Expected form-encoded webhook')
        if webhook_intake.locked():raise HTTPException(429,'Another webhook is being received')
        await webhook_intake.acquire()
        async def receive_body():
            chunks=[];total=0
            async for chunk in request.stream():
                total+=len(chunk)
                if total>65536:raise HTTPException(413,'Webhook too large')
                chunks.append(chunk)
            return b''.join(chunks)
        try:
            body=await asyncio.wait_for(receive_body(),timeout=WEBHOOK_RECEIVE_SECONDS)
        except asyncio.TimeoutError:
            raise HTTPException(408,'Webhook receive timeout') from None
        finally:webhook_intake.release()
        try:
            raw=body.decode('utf-8','strict')
            if request.scope.get('query_string') or re.search(r'%(?![0-9a-fA-F]{2})',raw):raise ValueError
            pairs=parse_qsl(raw,keep_blank_values=True,strict_parsing=True,encoding='utf-8',errors='strict',max_num_fields=100)
            form=dict(pairs)
            if len(form)!=len(pairs) or any(not k or len(k)>128 or len(v)>8192 for k,v in pairs):raise ValueError
        except (ValueError,UnicodeError):raise HTTPException(400,'Invalid webhook form') from None
        if not validate_twilio_signature(rt.config.auth_token,url,form,signature or ''):
            raise HTTPException(401,'Invalid webhook signature')
        return form

    @app.get('/health')
    def health():
        if rt is None:return {'status':'unavailable' if unavailable else 'disabled','owner_only':True,'live_test_ready':False}
        try:
            rt.assert_active()
            with rt.store._connect() as db:
                exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pilot_review_heartbeat'").fetchone()
                row=db.execute('SELECT updated_at FROM pilot_review_heartbeat WHERE approval_id=?',(rt.approval_id,)).fetchone() if exists else None
                cost_ready=components.cost_monitor.cost_gate(db,rt.clock())
            worker_ready=bool(row and 0<=rt.clock()-row[0]<120)
            memory_ready=components.memory_guard_factory()('admission') is True
            app_ready=worker_ready and memory_ready
            ready=app_ready and cost_ready and components.whatsapp_enabled
            state="active" if ready else ("private_app_only" if app_ready and not components.whatsapp_enabled else "paused")
            return {'status':state,'owner_only':True,'live_test_ready':ready,
                    'private_app_ready':app_ready,'whatsapp_enabled':components.whatsapp_enabled,
                    'worker_ready':worker_ready,'cost_ready':cost_ready,'memory_ready':memory_ready}
        except (ProviderError,ValueError):
            return {'status':'expired_or_unavailable','owner_only':True,'live_test_ready':False}

    @app.post('/webhooks/twilio/whatsapp')
    async def inbound(request:Request,x_twilio_signature:str|None=Header(default=None)):
        active()
        if components.whatsapp_enabled is not True:
            raise HTTPException(503,'WhatsApp intake is paused for private app verification')
        form=await signed_form(request,x_twilio_signature,rt.config.webhook_url)
        # Observe all valid signed/account-bound inbound IDs independently of
        # admission: even rejected upstream messages may incur provider charges.
        if form.get('AccountSid')==rt.config.account_sid and form.get('To')=='whatsapp:+'+rt.config.business_sender:
            sid=form.get('MessageSid','')
            if re.fullmatch(r'(?:SM|MM)[0-9a-fA-F]{32}',sid):
                with rt.store._connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    components.cost_monitor.observe_inbound(db,sid,rt.clock())
        try:
            rt.store.ingest_twilio(form,rt.clock(),account_sid=rt.config.account_sid,business_sender=rt.config.business_sender,
                                  admission_hook=rt.provider.admit_inbound)
        except (ValueError,TypeError,ProviderError):raise HTTPException(400,'Invalid or over-limit owner input') from None
        return Response(ACK,media_type='application/xml')

    @app.post('/webhooks/twilio/status')
    async def status(request:Request,x_twilio_signature:str|None=Header(default=None)):
        configured() # Reconcile already-sent IDs even after the send epoch expires.
        form=await signed_form(request,x_twilio_signature,rt.config.callback_url)
        try:rt.store.twilio_status(form,rt.clock(),account_sid=rt.config.account_sid,business_sender=rt.config.business_sender)
        except (ValueError,TypeError,ProviderError):raise HTTPException(400,'Invalid status') from None
        return Response(status_code=204)

    @app.api_route('/media/twilio/{token}/{filename}',methods=['GET','HEAD'])
    def media(token:str,filename:str,request:Request):
        active()
        try:data=rt.publisher.get(token,filename,head=request.method=='HEAD')
        except (MediaGrantError,ProviderError,OSError,ValueError):raise HTTPException(404,'Media unavailable') from None
        return Response(b'' if request.method=='HEAD' else data,media_type='application/pdf',headers={
            'Content-Length':str(len(data)),'Content-Disposition':f'inline; filename="{filename}"'})

    @app.get('/synthetic-forecast.pdf')
    def fixture_pdf():
        active();return Response(rt.fixtures['application/pdf'],media_type='application/pdf',headers={'Content-Disposition':'attachment; filename="fictional-forecast.pdf"'})

    @app.get('/synthetic-forecast.png')
    def fixture_photo():
        active();return Response(rt.fixtures['image/png'],media_type='image/png',headers={'Content-Disposition':'attachment; filename="fictional-forecast.png"'})
    return app


def from_environment():
    try:return create_app(build_owner_components())
    except Exception:
        # Startup readiness must fail closed without exposing config/credentials.
        return create_app(unavailable=True)


app=from_environment()
