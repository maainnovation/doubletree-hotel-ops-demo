"""Explicitly time-limited, single-sender, exact-fixture LIVE transport pilot.

Not production authorization or a real-hotel adapter. Only the generated synthetic
PDF can reach the parser. Existing development storage is usable solely within
this separately approved, persistent-volume, bounded-message experiment.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import fcntl
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit

import httpx
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import landscape, letter

from services.hotel_parameters import apply_shared_parameters
from services.staff_settings import demo_defaults
from services.twilio_media import TwilioMediaPublisher
from services.twilio_provider import TwilioWhatsAppProvider, twilio_business_id, ProviderResourceNotFound
from services.whatsapp_provider import ProviderError
from services.whatsapp_store import PrivateFileStore, WorkflowStore

TENANT = 'isolated-synthetic-pilot'
MANAGER = 'isolated-synthetic-manager'
HOTEL_CODE = 'SYNTHETIC-PILOT'
HOTEL_NAME = 'Fictional WhatsApp Pilot Hotel'
MAX_JOBS = 10
MAX_SEND_ATTEMPTS = 3
MAX_INBOUND_BYTES = 1024 * 1024


class PilotConfigurationError(ValueError):
    pass


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def synthetic_settings():
    return apply_shared_parameters(demo_defaults(), {'room_capacity':292})


def synthetic_forecast(week_start: date) -> bytes:
    out=BytesIO()
    pdf=canvas.Canvas(out,pagesize=landscape(letter),invariant=1)
    pdf.setTitle('Fictional WhatsApp integration test - not a real hotel forecast')
    lines=['SYNTHETIC TEST ONLY - fictional hotel, counts and staff',f'Hotel code: {HOTEL_CODE}',
           'Dates: '+' '.join((week_start+timedelta(days=i)).isoformat() for i in range(7))]
    metrics={'Departures':200,'Stayovers':70,'Arrivals':40,'Rooms on books':110,
             'Forecast pickup':10,'Forecast occupied':120,'Occupancy %':round(120/292*100,2)}
    lines += [label+': '+' '.join(str(value) for _ in range(7)) for label,value in metrics.items()]
    for i,line in enumerate(lines):
        pdf.drawString(25,560-26*i,line)
    pdf.save()
    return out.getvalue()


@dataclass(frozen=True)
class PilotConfig:
    account_sid: str
    auth_token: str = field(repr=False)
    business_sender: str
    manager_phone: str
    origin: str
    data_root: Path
    approved_until: float
    week_start: date

    @classmethod
    def from_env(cls, env=None, *, now=None):
        env=os.environ if env is None else env
        now=time.time() if now is None else now
        if env.get('PILOT_ENABLED')!='1' or env.get('PILOT_VOLUME_CONFIRMED')!='1':
            raise PilotConfigurationError('Explicit synthetic-pilot and persistent-volume approval are required.')
        try:
            account=env['TWILIO_ACCOUNT_SID']
            token=env['TWILIO_AUTH_TOKEN']
            business=env['TWILIO_WHATSAPP_FROM']
            phone_id=twilio_business_id(account,business)
            manager=env['PILOT_MANAGER_PHONE']
            if not re.fullmatch(r'[1-9][0-9]{6,14}',manager):
                raise ValueError
            origin=env['PILOT_EXTERNAL_ORIGIN']
            parsed=urlsplit(origin)
            if (parsed.scheme!='https' or not re.fullmatch(r'[a-z0-9-]+\.onrender\.com',parsed.netloc)
                    or parsed.path not in {'','/'} or parsed.query or parsed.fragment):
                raise ValueError
            expiry=datetime.fromisoformat(env['PILOT_APPROVED_UNTIL'].replace('Z','+00:00'))
            if expiry.tzinfo is None or not 0<expiry.timestamp()-now<=86400:
                raise ValueError
            week=date.fromisoformat(env['PILOT_WEEK_START'])
            today=datetime.fromtimestamp(now,timezone.utc).date()
            if week.weekday()!=4 or not week<=today<=week+timedelta(days=6):
                raise ValueError
            root=Path(env.get('PILOT_DATA_ROOT','/var/data/whatsapp-pilot'))
            if not root.is_absolute() or '..' in root.parts or root.is_symlink():
                raise ValueError
            if not token or len(token)>256 or any(c.isspace() for c in token):
                raise ValueError
        except (KeyError,TypeError,ValueError,ProviderError):
            raise PilotConfigurationError('Synthetic pilot configuration is missing, invalid, expired or not for the current week.') from None
        return cls(account,token,phone_id.rsplit(':',1)[1],manager,origin.rstrip('/'),root,expiry.timestamp(),week)

    @property
    def phone_id(self):
        return twilio_business_id(self.account_sid,self.business_sender)

    @property
    def webhook_url(self):
        return self.origin+'/webhooks/twilio/whatsapp'

    @property
    def callback_url(self):
        return self.origin+'/webhooks/twilio/status'

    def public_identity(self):
        # Hash persisted in private state; no credential values in DB/logs.
        return {'account':self.account_sid,'business':self.business_sender,'sender':self.manager_phone,
                'origin':self.origin,'until':self.approved_until,'week':self.week_start.isoformat()}


def _persistent_mount(root):
    if root.parent != Path('/var/data') or root.name != 'whatsapp-pilot':
        return False
    if os.path.ismount('/var/data'):
        return True
    try:
        return any(line.split()[4]=='/var/data' for line in Path('/proc/self/mountinfo').read_text().splitlines())
    except (OSError,IndexError):
        return False


class SyntheticPilotProvider:
    """Transport wrapper enforcing exact bytes, immutable demo state and send budget."""
    def __init__(self, transport_provider, store, files, config, fixture, *, clock=time.time):
        self.transport_provider,self.store,self.files=transport_provider,store,files
        self.config,self.fixture,self.clock=config,fixture,clock
        self.phone_number_id=config.phone_id
        self.is_live=transport_provider.is_live
        self.fixture_hash=hashlib.sha256(fixture).hexdigest()
        self.settings_hash=_fingerprint(synthetic_settings())

    def assert_active(self):
        if self.clock()>=self.config.approved_until:
            raise ProviderError('Synthetic pilot approval has expired; no messages may be sent.')
        saved=self.store.get_staff_settings(MANAGER)
        if not saved or saved.get('_dataset_mode')!='demo' or _fingerprint({k:v for k,v in saved.items() if not k.startswith('_')})!=self.settings_hash:
            raise ProviderError('Synthetic pilot settings changed; operation is blocked.')
        enrolled=self.store.enrollment(self.phone_number_id,self.config.manager_phone)
        if not enrolled or any(enrolled[k]!=v for k,v in {'tenant_id':TENANT,'manager_id':MANAGER,'hotel_code':HOTEL_CODE,'hotel_name':HOTEL_NAME,'report_profile':'canonical_v1'}.items()):
            raise ProviderError('Synthetic pilot enrollment changed; operation is blocked.')

    def permits_development_storage(self, store, files):
        self.assert_active()
        return store is self.store and files is self.files

    def verify_inbound_message(self,message_sid,sender,now):
        self.assert_active()
        if sender!=self.config.manager_phone:
            raise ProviderError('Sender is not enrolled for this isolated pilot.')
        try:
            return self.transport_provider.verify_inbound_message(message_sid,sender,now)
        except ProviderResourceNotFound:
            # Some trial Sandbox accounts expose Media metadata but return 404
            # for its parent Message. Never fall back on a mismatched Message,
            # stale timestamp, 401, redirect, malformed response or network error.
            return self._attest_fresh_pilot_media(message_sid,sender,now)

    def _attest_fresh_pilot_media(self,message_sid,sender,now):
        # The signed webhook established sender/business/tenant binding. Only an
        # existing claimed job can supply the URL; never accept one from callers.
        with self.store._connect() as db:
            db.row_factory=sqlite3.Row
            row=db.execute('SELECT * FROM wa_jobs WHERE phone_number_id=? AND message_id=? AND sender=? AND tenant_id=? AND status=?',
                           (self.phone_number_id,message_sid,sender,TENANT,'processing')).fetchone()
        if not row or not row['claim_token']:
            raise ProviderError('No claimed signed pilot input exists for media attestation.')
        try:
            media=json.loads(row['media_json'])
            enrolled=self.store.enrollment(self.phone_number_id,sender)
            if (not enrolled or media.get('_provider')!='twilio' or media.get('type')!='document'
                    or media.get('mime')!='application/pdf'
                    or media.get('_enrollment_binding')!=self.store.enrollment_binding(enrolled)):
                raise ValueError
            timestamp=self.transport_provider.verify_inbound_media_metadata(media['id'],message_sid,now)
            prior=media.get('_pilot_attestation')
            receipt=row['inbound_at']
            if prior is not None:
                if (not isinstance(prior,dict) or prior.get('kind')!='authenticated_media_after_message_404'
                        or prior.get('window_anchor')!=row['inbound_at'] or prior.get('media_created_at')!=timestamp):
                    raise ValueError
                receipt=prior.get('first_received_at')
            if isinstance(receipt,bool) or not isinstance(receipt,(int,float)) or not math.isfinite(receipt):
                raise ValueError
            # This is ONLY a fresh, observed synthetic pilot upload. The Media
            # creation time is not claimed to be the WhatsApp arrival time.
            # Bound it to the signed first receipt and the explicit pilot epoch.
            floor=self.config.approved_until-86400
            if timestamp<floor or not 0<=now-timestamp<=1800 or abs(timestamp-receipt)>300:
                raise ValueError
            # Conservative pilot window ends five minutes BEFORE approval expiry,
            # never media_created+24h. Generic production verification is unchanged.
            anchor=min(timestamp,receipt,float(now),floor)-300
            if now-anchor>=86400:
                raise ValueError
        except (KeyError,TypeError,ValueError):
            raise ProviderError('Fresh signed pilot media attestation failed.') from None
        media['_pilot_attestation']={'kind':'authenticated_media_after_message_404',
                                     'media_created_at':timestamp,'first_received_at':receipt,'window_anchor':anchor}
        with self.store._connect() as db:
            # Keep receipt audit and conservative anchor crash-consistent; the
            # worker may safely write the same anchor again after this returns.
            updated=db.execute('UPDATE wa_jobs SET media_json=?,inbound_at=? WHERE id=? AND status=? AND claim_token=? AND media_json=?',
                               (json.dumps(media),anchor,row['id'],'processing',row['claim_token'],row['media_json'])).rowcount
            if updated!=1:
                raise ProviderError('Pilot claim changed during media attestation.')
        return anchor

    def fetch_media(self,media_id,expected_mime,max_bytes):
        self.assert_active()
        if expected_mime!='application/pdf':
            raise ProviderError('Only the supplied synthetic PDF is accepted in this isolated test.')
        data=self.transport_provider.fetch_media(media_id,expected_mime,min(max_bytes,MAX_INBOUND_BYTES))
        if hashlib.sha256(data).hexdigest()!=self.fixture_hash:
            raise ProviderError('Only the exact supplied synthetic PDF is accepted in this isolated test.')
        return data

    def _reserve_send(self,to,key):
        self.assert_active()
        if to!=self.config.manager_phone:
            raise ProviderError('Only the explicitly enrolled pilot recipient may be messaged.')
        with self.store._connect() as db:
            db.row_factory=sqlite3.Row
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM wa_outbox WHERE id=?',(key,)).fetchone()
            if not row or row['recipient']!=to or row['phone_number_id']!=self.phone_number_id or row['tenant_id']!=TENANT or row['status']!='sending':
                raise ProviderError('No authorized pilot outbox attempt is available.')
            if db.execute('SELECT COUNT(*) FROM pilot_send_attempts').fetchone()[0]>=MAX_SEND_ATTEMPTS:
                raise ProviderError('Synthetic pilot send budget exhausted; operator approval is required.')
            try:
                db.execute('INSERT INTO pilot_send_attempts(outbox_id,attempt) VALUES(?,?)',(key,row['attempts']))
            except sqlite3.IntegrityError:
                raise ProviderError('Pilot send attempt already reserved; reconcile before resending.') from None

    def send_text(self,to,text,idempotency_key):
        self._reserve_send(to,idempotency_key)
        return self.transport_provider.send_text(to,text,idempotency_key)

    def send_document(self,to,pdf_bytes,filename,caption,idempotency_key):
        self._reserve_send(to,idempotency_key)
        return self.transport_provider.send_document(to,pdf_bytes,filename,caption,idempotency_key)


@dataclass
class PilotRuntime:
    config: PilotConfig
    store: WorkflowStore
    files: PrivateFileStore
    publisher: TwilioMediaPublisher
    provider: SyntheticPilotProvider
    fixture: bytes
    clock: object

    def assert_active(self):
        self.provider.assert_active()


def build_runtime(config: PilotConfig, *, transport=None, clock=time.time):
    # Revalidate even callers that constructed the dataclass directly. Importing
    # a config object is not approval for a longer-lived or differently scoped test.
    config=PilotConfig.from_env({'PILOT_ENABLED':'1','PILOT_VOLUME_CONFIRMED':'1',
        'TWILIO_ACCOUNT_SID':config.account_sid,'TWILIO_AUTH_TOKEN':config.auth_token,
        'TWILIO_WHATSAPP_FROM':config.business_sender,'PILOT_MANAGER_PHONE':config.manager_phone,
        'PILOT_EXTERNAL_ORIGIN':config.origin,'PILOT_DATA_ROOT':str(config.data_root),
        'PILOT_APPROVED_UNTIL':datetime.fromtimestamp(config.approved_until,timezone.utc).isoformat(),
        'PILOT_WEEK_START':config.week_start.isoformat()},now=clock())
    mocked=type(transport) is httpx.MockTransport
    if not mocked and not _persistent_mount(config.data_root):
        raise PilotConfigurationError('Live pilot requires the confirmed dedicated /var/data persistent mount.')
    root=config.data_root
    if root.is_symlink() or any(p.is_symlink() for p in root.parents):
        raise PilotConfigurationError('Pilot private storage cannot use symlink paths.')
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    os.chmod(root,0o700)
    for name in ('.init.lock','pilot.sqlite3','pilot.sqlite3-wal','pilot.sqlite3-shm','pilot.sqlite3-journal','private'):
        path=root/name
        if path.is_symlink() or (path.exists() and path.is_file() and path.stat().st_nlink!=1):
            raise PilotConfigurationError('Pilot state and lock files must not be links.')
    fixture=synthetic_forecast(config.week_start)
    identity=_fingerprint({**config.public_identity(),'fixture':hashlib.sha256(fixture).hexdigest(),'settings':_fingerprint(synthetic_settings())})
    # Two supervised processes may start together. Serialize nondestructive init.
    lockfd=os.open(root/'.init.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    with os.fdopen(lockfd,'a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        dbpath=root/'pilot.sqlite3'
        existed=dbpath.exists()
        if existed:
            with sqlite3.connect(dbpath) as db:
                try:
                    prior=db.execute('SELECT identity FROM pilot_identity WHERE id=1').fetchone()
                except sqlite3.Error:
                    raise PilotConfigurationError('Existing non-pilot state will not be adopted or overwritten.') from None
                if not prior or prior[0]!=identity:
                    raise PilotConfigurationError('Existing pilot identity/approval differs; no state was overwritten.')
        store=WorkflowStore(dbpath)
        if not existed:
            store.enroll_tenant(tenant_id=TENANT,manager_id=MANAGER,hotel_code=HOTEL_CODE,hotel_name=HOTEL_NAME)
            store.enroll_sender(tenant_id=TENANT,phone_number_id=config.phone_id,sender=config.manager_phone)
            store.save_staff_settings(MANAGER,synthetic_settings(),dataset_mode='demo')
            with store._connect() as db:
                db.executescript('CREATE TABLE pilot_identity(id INTEGER PRIMARY KEY,identity TEXT NOT NULL); CREATE TABLE pilot_send_attempts(outbox_id TEXT NOT NULL,attempt INTEGER NOT NULL,PRIMARY KEY(outbox_id,attempt)); CREATE TABLE pilot_heartbeat(id INTEGER PRIMARY KEY,updated_at REAL NOT NULL);')
                db.execute('INSERT INTO pilot_identity(id,identity) VALUES(1,?)',(identity,))
        files=PrivateFileStore(root/'private')
    publisher=TwilioMediaPublisher(store,files,config.origin,clock=clock)
    actual=TwilioWhatsAppProvider(config.account_sid,config.auth_token,config.business_sender,media_publisher=publisher,
                                 status_callback_url=config.callback_url,production_approved=not mocked,transport=transport)
    provider=SyntheticPilotProvider(actual,store,files,config,fixture,clock=clock)
    provider.assert_active()
    return PilotRuntime(config,store,files,publisher,provider,fixture,clock)
