import json
from odin.db import session
from odin.models import (Case, Olt, OltOutage, OltPonPort, Onu, ProbeRollup, ProbeSeries, ProbeState, ProbeTarget, now, uid)
from odin.inventory.connectors import ConnectorError
from odin.probes.config import ProbeConfig, DEFAULT_VERSION_ID
from odin.analyst import planner
from test_access import enroll
from test_analyst import llm, ask, chat_answer, one_case, run_jobs  # noqa: F401  (llm is a fixture)


def plan(topics, **names):
    return json.dumps({'temas': topics, **{f: names.get(f, []) for f in planner.FIELDS}})


def target(db, name, service, categories, mean, loss=0.0, protocol='icmp'):
    """A destination with one hour of 5-minute aggregates at `mean` ms and `loss` percent."""
    row = ProbeTarget(id=uid(), name=name, address='203.0.113.9', family='ipv4', protocol=protocol, profile_version_id=DEFAULT_VERSION_ID,
                      approved_ips=[], effective=ProbeConfig().model_dump(), enabled=True, service=service, categories=categories)
    db.add(row);db.flush()
    stamp = now() // 300 * 300
    series = ProbeSeries(target_id=row.id, address=row.address, epoch=1, started_at=stamp - 7200, config={'interval_seconds': 60})
    db.add(series);db.flush()
    for bucket in range(stamp - 3600, stamp, 300):
        received = round(10 * (1 - loss / 100))
        db.add(ProbeRollup(series_id=series.id, bucket=bucket, resolution=300, expected=5, valid=5, sent=10, received=received,
                           rtt_sum=mean * received, rtt_min=mean - 5, rtt_max=mean + 5))
    db.add(ProbeState(target_id=row.id, address=row.address, series_id=series.id, conditions={}, counters={},
                      metrics={'state': 'healthy', 'p95_ms': mean + 2}))
    return row


def conversation(client):
    enroll(client)
    return client.post('/api/v1/conversations', json={}).json()['id']


def test_a_latency_question_gets_the_measurements_of_what_it_names(client, administrator, llm):
    one_case()  # a BGP case of the period: it has nothing to do with games
    with session() as db:
        target(db, 'FREEFIRE-1', 'Free Fire', ['juegos'], 40)
        target(db, 'RIOT-1', 'Riot', ['juegos'], 70, loss=10)
        target(db, 'NETFLIX-WEB', 'Netflix', ['streaming'], 30, protocol='https')
        t = now() - 1800
        for service in ('Free Fire', 'Netflix'):
            db.add(Case(kind='latency', key=uid(), entity_key='service:' + service, entity_label=service, subject=service, opened_at=t,
                        last_activity_at=t, last_state='down', last_state_at=t, state='active', severity='warning', rule_version=4,
                        detail={'service': service, 'scope': 'external'}))
        db.commit()
    chat = conversation(client)
    llm.plans = [plan(['latencia'], categorias=['Juegos'])]
    llm.answers = [chat_answer('Media de 55 ms [D1].', ('D1',))]
    ask(client, chat, '¿Qué latencia tenemos a los juegos?', hours=6);run_jobs()
    index = llm.plan_calls[0][1]['content']
    assert '- juegos: Free Fire, Riot' in index and 'Pregunta: «¿Qué latencia tenemos a los juegos?»' in index
    context = llm.calls[0][1]['content']
    assert 'Tema de la pregunta: latencia (categorias: juegos).' in context
    assert ('D1 · categoría juegos · 2 destinos · período: latencia media 55,0 ms (entre 40,0 ms y 70,0 ms), peor RIOT-1 con 70,0 ms'
            ' · con pérdida o fallos: RIOT-1 10,0 % · ahora: normal: 2') in context
    assert 'destino FREEFIRE-1 (Free Fire) · juegos · ICMP' in context and 'latencia media 40,0 ms, máxima 45,0 ms, pérdida 0,0 %' in context
    # The latency case of a game comes along; not other services, the BGP case or the logs of the period.
    assert 'C1 · caso de latencia hacia Free Fire' in context and 'C2 ·' not in context
    assert 'NETFLIX' not in context and 'Netflix' not in context and 'sesión BGP' not in context and 'G1 ·' not in context
    run = client.get(f'/api/v1/conversations/{chat}').json()['messages'][0]['run']
    assert run['status'] == 'done' and run['refs']['D1']['kind'] == 'probe' and run['request']['plan']['categorias'] == ['juegos']


def test_an_access_question_gets_the_olt_and_its_pon_outages(client, administrator, llm):
    one_case()
    stamp = now()
    with session() as db:
        db.add(Olt(id='1', name='OLT_5800X2', hardware='Huawei-MA5800-X2', ip='10.11.104.2', last_seen=stamp, temperature=36));db.flush()
        db.add_all([OltPonPort(olt_id='1', board='1', port='0', admin_status='Enabled', status='Up / Autofind', onus=20, online=20, last_seen=stamp),
                    OltPonPort(olt_id='1', board='1', port='1', admin_status='Enabled', status='Down / Autofind', onus=8, online=0, last_seen=stamp)])
        db.add_all([Onu(id=f'o{n}', olt_id='1', board='1', port='0', status='Online', signal='Very good', last_seen=stamp) for n in range(3)])
        db.add(Onu(id='o9', olt_id='1', board='1', port='1', status='LOS', signal='', last_seen=stamp))
        db.add(OltOutage(olt_id='1', board='1', port='1', cause='los', zone='Zona - El Centro', subscribers=8, affected=8, started_at=stamp - 1200,
                         first_seen=stamp - 1200, last_seen=stamp))
        db.commit()
    chat = conversation(client)
    llm.plans = [plan(['acceso'], olts=['olt_5800x2'])]
    llm.answers = [chat_answer('El PON 1/1 está caído [O2].', ('O2',))]
    ask(client, chat, '¿Cómo están los clientes de fibra?', hours=6);run_jobs()
    context = llm.calls[0][1]['content']
    assert ('O1 · OLT OLT_5800X2 (Huawei-MA5800-X2, 10.11.104.2) · ahora: alcanzable · ONUs en línea 20 de 28 · ONUs por estado: Online 3, LOS 1'
            in context) and 'PON caídos: 1/1 de 2 con ONUs' in context and 'temperatura 36 °C' in context
    assert 'O2 · OLT OLT_5800X2 · PON 1/1: corte de fibra (LOS) · 8 de 8 ONUs · zona Zona - El Centro' in context and '(sigue activa)' in context
    assert 'C1 ·' not in context and 'D1 ·' not in context


def test_plan_names_are_matched_to_the_index_and_anything_else_is_dropped():
    idx = {'categorias': {'juegos': ['Free Fire']}, 'servicios': ['Free Fire'], 'destinos': ['FREEFIRE-1'], 'equipos': ['borde-sertelcon'],
           'olts': ['OLT_5800X2'], 'pops': []}
    parsed = planner.parse(plan(['latencia', 'inventado'], servicios=['free fire', 'Fortnite'], equipos=['BORDE-SERTELCON'], olts=['otra']), idx)
    assert parsed == {'temas': ['latencia'], 'categorias': [], 'servicios': ['Free Fire'], 'destinos': [], 'equipos': ['borde-sertelcon'], 'olts': []}
    assert planner.parse('no es json', idx) is None and planner.parse(plan(['inventado']), idx) is None


def test_a_failed_plan_falls_back_to_the_general_context(client, administrator, llm):
    one_case()
    chat = conversation(client)
    llm.plans = [ConnectorError('server_unavailable')]
    llm.answers = [chat_answer()]
    ask(client, chat);run_jobs()
    context = llm.calls[0][1]['content']
    assert 'Tema de la pregunta: general.' in context and 'C1 · caso de sesión BGP' in context


def test_a_follow_up_on_another_topic_gets_a_new_context(client, administrator, llm):
    one_case()
    with session() as db:
        target(db, 'FREEFIRE-1', 'Free Fire', ['juegos'], 40);db.commit()
    chat = conversation(client)
    llm.plans = [plan(['general']), plan(['latencia'], servicios=['Free Fire'])]
    llm.answers = [chat_answer(), chat_answer('40 ms [D2].', ('D2',))]
    ask(client, chat);run_jobs()
    ask(client, chat, '¿Y la latencia a Free Fire?', 'mensaje-0002');run_jobs()
    first, second = llm.calls
    assert second[1] != first[1] and 'destino FREEFIRE-1' in second[1]['content'] and 'C1 ·' not in second[1]['content']
    # The planner read the earlier turn to understand the follow-up.
    assert 'Operador: ¿Qué pasó?' in llm.plan_calls[1][1]['content']
