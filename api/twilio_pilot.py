"""Minimal separately deployed synthetic pilot surface; no hotel app/admin routes."""
from collections import deque
from contextlib import asynccontextmanager
import os
import re
import time
from urllib.parse import parse_qsl

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import Response

from services.twilio_media import MediaGrantError
from services.twilio_pilot import PilotConfig, build_runtime, MAX_JOBS
from services.twilio_provider import validate_twilio_signature
from services.whatsapp_provider import ProviderError


def create_app(runtime=None):
    @asynccontextmanager
    async def lifespan(application):
        application.state.runtime=runtime
        if runtime is None and os.getenv('PILOT_ENABLED','0')=='1':
            application.state.runtime=build_runtime(PilotConfig.from_env())
        yield

    app=FastAPI(title='Isolated synthetic WhatsApp pilot',docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)
    requests=deque()

    @app.middleware('http')
    async def request_limits(request,call_next):
        # Public synthetic fixture is tiny; bound service-level egress/request work.
        # Render-level DDoS controls are still required. Never log token paths/body.
        now=time.monotonic()
        while requests and requests[0]<now-60:
            requests.popleft()
        if len(requests)>=60:
            return Response(status_code=429,headers={'Retry-After':'60','Cache-Control':'no-store'})
        requests.append(now)
        response=await call_next(request)
        response.headers['Cache-Control']='private, no-store, max-age=0'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['X-Content-Type-Options']='nosniff'
        return response

    def active():
        value=getattr(app.state,'runtime',None)
        if value is None:
            raise HTTPException(503,'Synthetic pilot is disabled')
        try:
            value.assert_active()
        except ProviderError:
            raise HTTPException(503,'Synthetic pilot is unavailable or expired') from None
        return value

    async def signed_form(request,signature,url):
        if request.headers.get('content-type','').split(';',1)[0].lower()!='application/x-www-form-urlencoded':
            raise HTTPException(415,'Expected form-encoded webhook')
        chunks,total=[],0
        async for chunk in request.stream():
            total+=len(chunk)
            if total>65536:
                raise HTTPException(413,'Webhook body too large')
            chunks.append(chunk)
        try:
            raw=b''.join(chunks).decode('utf-8','strict')
            if re.search(r'%(?![0-9a-fA-F]{2})',raw) or request.scope.get('query_string'):
                raise ValueError
            pairs=parse_qsl(raw,keep_blank_values=True,strict_parsing=True,encoding='utf-8',errors='strict',max_num_fields=100)
            form=dict(pairs)
            if len(form)!=len(pairs) or any(not key or len(key)>128 or len(value)>8192 for key,value in pairs):
                raise ValueError
        except (ValueError,UnicodeError):
            raise HTTPException(400,'Invalid webhook form') from None
        rt=active()
        if not validate_twilio_signature(rt.config.auth_token,url,form,signature or ''):
            raise HTTPException(401,'Invalid webhook signature')
        return rt,form

    @app.get('/health')
    def health():
        rt=getattr(app.state,'runtime',None)
        if rt is None:
            return {'status':'disabled','synthetic_only':True,'live_test_ready':False}
        active()
        with rt.store._connect() as db:
            row=db.execute('SELECT updated_at FROM pilot_heartbeat WHERE id=1').fetchone()
        ready=bool(row and 0<=rt.clock()-row[0]<120)
        if not ready:
            raise HTTPException(503,'Synthetic pilot worker is not ready')
        return {'status':'active','synthetic_only':True,'live_test_ready':True}

    @app.get('/synthetic-forecast.pdf')
    def fixture():
        rt=active()
        return Response(rt.fixture,media_type='application/pdf',headers={'Content-Disposition':'attachment; filename="synthetic-test.pdf"'})

    @app.post('/webhooks/twilio/whatsapp')
    async def inbound(request:Request,x_twilio_signature:str|None=Header(default=None)):
        rt=active()
        rt,form=await signed_form(request,x_twilio_signature,rt.config.webhook_url)
        try:
            rt.store.ingest_twilio(form,rt.clock(),account_sid=rt.config.account_sid,business_sender=rt.config.business_sender,max_jobs=MAX_JOBS)
        except (ValueError,TypeError,ProviderError):
            raise HTTPException(400,'Invalid or over-limit pilot message') from None
        return Response('<?xml version="1.0" encoding="UTF-8"?><Response/>',media_type='application/xml')

    @app.post('/webhooks/twilio/status')
    async def status(request:Request,x_twilio_signature:str|None=Header(default=None)):
        rt=active()
        rt,form=await signed_form(request,x_twilio_signature,rt.config.callback_url)
        try:
            rt.store.twilio_status(form,rt.clock(),account_sid=rt.config.account_sid,business_sender=rt.config.business_sender)
        except (ValueError,TypeError,ProviderError):
            raise HTTPException(400,'Invalid pilot status') from None
        return Response(status_code=204)

    @app.api_route('/media/twilio/{token}/{filename}',methods=['GET','HEAD'])
    def media(token:str,filename:str,request:Request):
        rt=active()
        try:
            data=rt.publisher.get(token,filename,head=request.method=='HEAD')
        except (MediaGrantError,OSError):
            raise HTTPException(404,'Media unavailable') from None
        return Response(b'' if request.method=='HEAD' else data,media_type='application/pdf',headers={
            'Content-Length':str(len(data)),'Content-Disposition':f'inline; filename="{filename}"'})

    return app


app=create_app()
