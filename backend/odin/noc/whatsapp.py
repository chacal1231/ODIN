"""The NOC group on WhatsApp, through ODIN's WhatsApp service (whatsapp/, whatsapp-web.js).

An administrator pairs a WhatsApp account by scanning the QR shown in ODIN and picks one of its groups. ODIN then posts
there what it posts to the Telegram NOC group: each case when it starts alerting, its diagnosis and its recovery, quoting
the case message so each case reads as a thread, plus the level-1 reports and the shift handovers. WhatsApp is outbound
only: rating diagnoses and asking ODIN stay in ODIN and Telegram.
The service is reached inside the Compose network with a token derived from the master key.
What is sent leaves the network, as with Telegram: names, addresses and readings of cases.
"""
import hashlib
import hmac
import logging
from pathlib import Path
import httpx
from sqlalchemy import select
from ..config import settings
from ..db import session
from ..models import Analysis, Case, CaseReport, ShiftReport, WhatsAppMessage, WhatsAppState, now
from . import messages, reports

TIMEOUT = 30
RENOTIFY = 600


class WhatsAppError(Exception):
    def __init__(self, code):
        super().__init__(code);self.code = code


def token():
    key = Path(settings().master_key_file).read_bytes().strip()
    return hmac.new(key, b'odin-whatsapp', hashlib.sha256).hexdigest()


class Service:
    def __init__(self, url=None):
        self.url = (url or settings().whatsapp_url).rstrip('/')

    def call(self, method, path, payload=None):
        try:
            response = httpx.request(method, self.url + path, json=payload, timeout=TIMEOUT,
                                     headers={'Authorization': 'Bearer ' + token()})
        except httpx.HTTPError:
            raise WhatsAppError('whatsapp_unreachable')
        data = response.json() if response.headers.get('content-type', '').startswith('application/json') else {}
        if response.status_code != 200:raise WhatsAppError((data.get('error') or f'http_{response.status_code}')[:80])
        return data

    def status(self):
        return self.call('GET', '/status')

    def groups(self):
        return self.call('GET', '/groups')

    def send(self, group, text, quoted=None):
        return self.call('POST', '/send', {'chat_id': group, 'text': text, 'quoted': quoted})['id']

    def logout(self):
        return self.call('POST', '/logout')


def state(db):
    row = db.get(WhatsAppState, 'whatsapp')
    if not row:row = WhatsAppState(id='whatsapp', paused=False);db.add(row);db.flush()
    return row


def outbound(db, service, st):
    from ..cases.summary import alerting, external
    stamp = now()
    known = set(db.scalars(select(WhatsAppMessage.case_id)))
    new = [c for c in alerting(db) if c.id not in known]
    groups = [[c] for c in new if not external(c)] + ([[c for c in new if external(c)]] if any(external(c) for c in new) else [])
    for cases in groups:
        message = messages.external_group(cases) if external(cases[0]) else messages.case_alert(cases[0])
        sent = service.send(st.group_id, messages.whatsapp(message))
        for case in cases:db.add(WhatsAppMessage(case_id=case.id, group_id=st.group_id, message_id=sent, notified_at=stamp))
        db.commit()
    for row, case in db.execute(select(WhatsAppMessage, Case).join(Case, Case.id == WhatsAppMessage.case_id)
                                .where((WhatsAppMessage.analysis_id.is_(None)) | (Case.state != 'closed') | (WhatsAppMessage.recovered.is_(False)))).all():
        if row.analysis_id is None:
            analysis = db.scalar(select(Analysis).where(Analysis.case_id == case.id, Analysis.kind == 'case', Analysis.status == 'done')
                                 .order_by(Analysis.finished_at.desc()).limit(1))
            if analysis:
                service.send(row.group_id, messages.whatsapp(messages.diagnosis(case, analysis, buttons=False)), row.message_id)
                row.analysis_id = analysis.id;db.commit()
        if not row.recovered and (case.last_state == 'up' or case.state == 'closed'):
            service.send(row.group_id, messages.whatsapp(messages.recovered(case)), row.message_id)
            row.recovered = True;db.commit()
        elif row.recovered and case.state == 'active' and case.last_state == 'down' and stamp - row.notified_at > RENOTIFY:
            service.send(row.group_id, messages.whatsapp(messages.dropped_again(case)), row.message_id)
            row.recovered, row.notified_at = False, stamp;db.commit()


def deliver(db, service, st):
    """Level-1 reports and shift handovers queued for WhatsApp."""
    for row in reports.pending(db, CaseReport, 'whatsapp'):
        case = db.get(Case, row.case_id)
        thread = db.get(WhatsAppMessage, row.case_id)
        try:
            sent = service.send(st.group_id, messages.whatsapp(messages.report(row, case, reports.author(db, row))), thread.message_id if thread else None)
            reports.delivered(row, 'whatsapp', sent)
        except WhatsAppError as exc:
            if exc.code in ('whatsapp_unreachable', 'not_ready'):raise
            reports.delivered(row, 'whatsapp', error=exc.code)
        db.commit()
    for row in reports.pending(db, ShiftReport, 'whatsapp'):
        try:reports.delivered(row, 'whatsapp', service.send(st.group_id, messages.whatsapp(messages.shift(row))))
        except WhatsAppError as exc:
            if exc.code in ('whatsapp_unreachable', 'not_ready'):raise
            reports.delivered(row, 'whatsapp', error=exc.code)
        db.commit()


def cycle(service=None):
    with session() as db:
        st = state(db);st.heartbeat_at = now();db.commit()
        if not st.group_id or st.paused:return
        service = service or Service()
        try:
            outbound(db, service, st)
            deliver(db, service, st)
            st.last_error = None
        except WhatsAppError as exc:
            db.rollback();st = state(db);st.last_error = exc.code
            logging.warning('whatsapp_failed %s', exc.code)
        db.commit()
