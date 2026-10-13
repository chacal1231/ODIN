import datetime as dt
from zoneinfo import ZoneInfo
from sqlalchemy import select
from odin.db import session
from odin.models import (Analysis, Case, CaseNote, CaseReport, Integration, ShiftReport, TelegramState, User, WhatsAppMessage,
                         WhatsAppState, now, uid)
from odin.noc import guides, messages, reports, whatsapp
from odin.inbox import build
from odin import telegram
from test_access import enroll
from test_telegram import Bot

ZONE = ZoneInfo('America/Bogota')


def at(day, clock):
    return int(dt.datetime.combine(dt.date(2026, 10, day), dt.time.fromisoformat(clock), ZONE).timestamp())


def access_case(db, opened, code='fiber', state='down', **detail):
    row = Case(kind='access', key=uid(), entity_key='olt:1', entity_label='POP Centro', subject='OLT-NORTE', opened_at=opened,
               last_activity_at=opened, last_state=state, last_state_at=opened, state='active', severity='warning', rule_version=4,
               detail={'scope': 'access', 'olt_id': '1', 'zones': ['Norte'],
                       'reading': {'code': code, 'title': 'Corte de fibra en el PON 0/1: 20 de 32 ONUs (zona Norte)',
                                   'action': 'Informar corte de fibra en Norte. N2 despacha a campo.'},
                       'impact': {'kind': 'confirmed', 'onus': 20, 'text': 'Confirmado: 20 ONUs afectadas'}, **detail})
    db.add(row);db.flush();return row


def user(db, role='operator'):
    row = User(username='n1-' + uid()[:6], role=role, password_hash='x');db.add(row);db.flush();return row


def channels(db):
    db.add(Integration(kind='telegram', endpoint='https://api.telegram.org', encrypted_secret='x', allowed_networks=['0.0.0.0/0']))
    db.add(TelegramState(id='telegram', chat_id=-100, thread_id=None, update_offset=0, paused=False))
    db.add(WhatsAppState(id='whatsapp', group_id='123@g.us', group_name='NOC', paused=False));db.flush()


def test_guides_follow_the_situation_and_fill_the_report():
    with session() as db:
        case = access_case(db, at(12, '14:10'))
        assert guides.situation(case) == 'access_fiber'
        assert guides.draft(db, case, 'inicial', at(12, '14:20')) == (
            'Corte de fibra: OLT OLT-NORTE (POP Centro). Corte de fibra en el PON 0/1: 20 de 32 ONUs (zona Norte). '
            'Afectación: Confirmado: 20 ONUs afectadas. Inicio: 12/10 14:10.')
        assert guides.draft(db, case, 'restablecido', at(12, '15:25')) == 'Restablecido: OLT OLT-NORTE (POP Centro). Duración: 1 h 15 min (desde 12/10 14:10).'
        external = Case(kind='latency', subject='Netflix', entity_label='Netflix', detail={'scope': 'external', 'verdict': {'state': 'service'}})
        assert guides.situation(external) == 'external_service'
        assert guides.situation(Case(kind='bgp_session', detail={})) == 'network_bgp'


def test_deadline_escalation_and_reports_go_to_every_channel():
    with session() as db:
        channels(db);n1 = user(db)
        case = access_case(db, now() - 11 * 60)
        state = reports.status(db, [case])[case.id]
        assert state['overdue'] and not state['escalate_due'] and state['reported_at'] is None
        row = reports.create(db, case, 'inicial', 'Corte de fibra en Norte.', n1);db.commit()
        assert row.deliveries == {'telegram': {'status': 'pending'}, 'whatsapp': {'status': 'pending'}}
        assert db.scalar(select(CaseNote.text)) == 'Informe N1 (inicial): Corte de fibra en Norte.'
        state = reports.status(db, [case])[case.id]
        assert not state['overdue'] and state['reported_at']
        # The fiber guide escalates at 15 minutes.
        case.opened_at = now() - 16 * 60
        assert reports.status(db, [case])[case.id]['escalate_due']
        try:
            reports.create(db, case, 'escalado', guides.draft(db, case, 'escalado', now()), n1)
            raise AssertionError('an escalation needs its reason')
        except ValueError:pass


def test_whatsapp_posts_case_threads_reports_and_shifts():
    class Service:
        def __init__(self):self.sent, self.n = [], 0
        def send(self, group, text, quoted=None):
            self.n += 1;self.sent.append((group, text, quoted));return f'msg{self.n}'
    with session() as db:
        channels(db);n1 = user(db)
        case = access_case(db, now() - 600);case.severity = 'critical'
        db.commit()
    service = Service()
    whatsapp.cycle(service)
    alert = service.sent[0][1]
    assert alert.startswith('*[CRÍTICO]* Corte de fibra · OLT-NORTE · POP Centro\n*Desde:* ')
    assert '*Afectación:* Confirmado: 20 ONUs afectadas' in alert and '*Acción:* informar corte de fibra en Norte; N2 despacha a campo' in alert
    assert 'Ref.: http' in alert
    with session() as db:
        case = db.get(Case, db.scalar(select(Case.id)))
        reports.create(db, case, 'inicial', 'Corte de fibra en Norte.', db.get(User, n1.id))
        db.add(Analysis(id=uid(), kind='case', case_id=case.id, status='done', finished_at=now(), created_at=now(), requested_by=n1.id,
                        result={'resumen': 'Corte en la zona Norte.', 'hipotesis': [{'hipotesis': 'Fibra cortada', 'confianza': 'alta'}]}))
        db.add(ShiftReport(id=uid(), kind='cierre', starts_at=now() - 3600, ends_at=now(), text='Casos abiertos al cierre: 1', data={},
                           created_at=now(), deliveries={'whatsapp': {'status': 'pending'}}))
        db.commit()
    whatsapp.cycle(service)
    texts = {text.split('\n')[0]: quoted for _, text, quoted in service.sent[1:]}
    assert texts['*[DIAGNÓSTICO ODIN]* Corte de fibra · OLT-NORTE · POP Centro'] == 'msg1'
    assert texts['*[INFORME N1]* Corte de fibra · OLT-NORTE · POP Centro'] == 'msg1'
    assert any(t.startswith('*[ENTREGA DE TURNO]*') for t in texts)
    with session() as db:
        assert db.scalar(select(CaseReport)).deliveries['whatsapp']['status'] == 'sent'
        assert db.get(WhatsAppMessage, case.id).analysis_id
    # A paused group posts nothing.
    with session() as db:
        db.get(WhatsAppState, 'whatsapp').paused = True;db.commit()
    count = len(service.sent);whatsapp.cycle(service)
    assert len(service.sent) == count


def test_telegram_delivers_reports_in_the_case_thread():
    with session() as db:
        channels(db);n1 = user(db)
        case = access_case(db, now() - 600);case.severity = 'critical';db.commit()
    bot = Bot()
    with session() as db:
        st = telegram.state(db);telegram.outbound(db, bot, st)
        case = db.get(Case, db.scalar(select(Case.id)))
        reports.create(db, case, 'inicial', 'Corte de fibra en Norte.', db.get(User, n1.id));db.commit()
        telegram.deliver(db, bot, st)
    alert, report = bot.sent()
    assert alert['text'].startswith('<b>[CRÍTICO]</b> Corte de fibra · OLT-NORTE')
    assert report['text'].startswith('<b>[INFORME N1]</b>') and report['reply_parameters']['message_id'] == 101


def test_shift_boundaries_handover_and_unwatched_hours():
    shifts = [{'start': '06:00', 'end': '20:00'}]
    closed, gap = reports.boundaries(shifts, at(12, '20:05'))
    assert closed == (at(12, '06:00'), at(12, '20:00')) and gap == (at(11, '20:00'), at(12, '06:00'))
    with session() as db:
        access_case(db, at(12, '14:10'), severity='critical');db.commit()
        made = reports.tick(db, at(12, '20:05'))
        assert [r.kind for r in made] == ['cierre']  # the night before is older than an hour
        assert reports.tick(db, at(12, '20:30')) == []
        text = db.scalar(select(ShiftReport.text))
        assert text.startswith('Casos abiertos al cierre: 1\n- [ADVERTENCIA] Corte de fibra · OLT-NORTE · POP Centro — desde 12/10 14:10')
        assert 'sin informar' in text and 'Informes N1: 0; 1 caso(s) informados fuera de plazo o sin informar' in text
        made = reports.tick(db, at(13, '06:10'))
        assert [(r.kind, r.starts_at, r.ends_at) for r in made] == [('apertura', at(12, '20:00'), at(13, '06:00'))]


def test_inbox_moves_reported_cases_to_progress_and_overdue_first(client, administrator):
    with session() as db:
        late = access_case(db, now() - 30 * 60)
        fresh = access_case(db, now() - 60, entity_key='olt:2');fresh.subject = 'OLT-SUR';fresh.detail = {**fresh.detail, 'olt_id': '2'}
        fresh.detail['impact'] = {'kind': 'confirmed', 'onus': 500, 'text': 'x'}
        db.commit()
        rows = [i for i in build(db)['attention'] if i['kind'] == 'case']
        # The late one goes first even with fewer customers.
        assert [r['title'] for r in rows] == ['Acceso OLT-NORTE', 'Acceso OLT-SUR'] and rows[0]['overdue']
        reports.create(db, late, 'inicial', 'Informado.', db.get(User, administrator));db.commit()
        data = build(db)
        assert [i['title'] for i in data['progress'] if i['kind'] == 'case'] == ['Acceso OLT-NORTE']
        assert data['counts']['Casos'] == 1
        case_id = fresh.id
    enroll(client)
    info = client.get(f'/api/v1/cases/{case_id}/communication').json()
    assert info['situation'] == 'access_fiber' and info['guide']['escalate_minutes'] == 15 and info['drafts']['inicial'].startswith('Corte de fibra')
    assert client.post(f'/api/v1/cases/{case_id}/reports', json={'kind': 'actualizacion', 'text': 'Sigue caído.'}).status_code == 201
    assert client.get(f'/api/v1/cases/{case_id}/communication').json()['reports'][0]['author'] == 'admin'


def test_settings_and_guides_api(client, administrator):
    enroll(client)
    assert client.get('/api/v1/noc/settings').json()['shifts'] == [{'start': '06:00', 'end': '20:00'}]
    assert client.put('/api/v1/noc/settings', json={'report_minutes': 5, 'shifts': [{'start': '06:00', 'end': '06:00'}]}).status_code == 422
    saved = client.put('/api/v1/noc/settings', json={'report_minutes': 5, 'shifts': [{'start': '07:00', 'end': '19:00'}]}).json()
    assert saved == {'report_minutes': 5, 'shifts': [{'start': '07:00', 'end': '19:00'}]}
    body = {'title': 'Corte', 'verify': ['Llamar a campo', ''], 'template': 'Corte en {lugar}', 'escalate': 'Siempre', 'escalate_minutes': 5}
    assert client.put('/api/v1/noc/guides/access_fiber', json={**body, 'template': 'Corte {'}).status_code == 422
    guide = client.put('/api/v1/noc/guides/access_fiber', json=body).json()
    assert guide['custom'] and guide['verify'] == ['Llamar a campo']
    assert not client.delete('/api/v1/noc/guides/access_fiber').json()['custom']


def test_messages_render_per_channel():
    message = messages.Message('CRÍTICO', 'Interfaz · Ethernet1 · PE-A', [('Lectura', 'tránsito *CARRIER* <1>')], link='https://odin/#caso=1')
    assert messages.whatsapp(message) == '*[CRÍTICO]* Interfaz · Ethernet1 · PE-A\n*Lectura:* tránsito CARRIER <1>\nRef.: https://odin/#caso=1'
    assert messages.telegram(message) == ('<b>[CRÍTICO]</b> Interfaz · Ethernet1 · PE-A\n<b>Lectura:</b> tránsito *CARRIER* &lt;1&gt;\n'
                                          '<a href="https://odin/#caso=1">Ref. en ODIN</a>')
