import socket
import struct
import threading
import pytest
from sqlalchemy import select
from odin.db import session
from odin.models import AutonomousSystem, ProbeTarget, ProbeIncident, ProbeTrace, Case, CaseEvidence, now
from odin.probes import asnames
from odin.cases import correlator
from odin.cases.entities import Entity
from test_probes import target

REAL_TXT = asnames.txt  # conftest replaces it during tests so nothing reaches the network


@pytest.mark.parametrize('name,key,display', [
    ('EDGEUNO S.A.S, CO', 'EDGEUNO', 'Edgeuno'),
    ('AMAZON-02 - Amazon.com, Inc., US', 'AMAZON', 'Amazon.com'),
    ('AKAMAI-ASN1, NL', 'AKAMAI', 'Akamai-Asn1'),
    ('AKAMAI-AS - Akamai Technologies, Inc., US', 'AKAMAI', 'Akamai Technologies'),
    ('CLOUDFLARESPECTRUM Cloudflare, Inc., US', 'CLOUDFLARE', 'Cloudflare'),
    ('TWELVE99 Arelion, fka Telia Carrier, SE', 'ARELION', 'Arelion Telia Carrier'),
    ('LVLT-3549 - Level 3 Parent, LLC, US', 'LEVEL', 'Level 3'),
    ('TV&MAS S.A.S, CO', 'TVMAS', 'Tv&Mas'),
])
def test_organization_joins_systems_of_one_company(name, key, display):
    assert asnames.organization(name) == (key, display)


def serve(answer_for):
    """A one-shot DNS server on loopback answering with the packet built from the query."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM);sock.bind(('127.0.0.1', 0))
    def run():
        query, peer = sock.recvfrom(512);sock.sendto(answer_for(query), peer);sock.close()
    threading.Thread(target=run, daemon=True).start()
    return sock.getsockname()


def txt_answer(query, text):
    question = query[12:]
    record = b'\xc0\x0c' + struct.pack('>HHIH', 16, 1, 60, len(text) + 1) + bytes([len(text)]) + text.encode()
    return query[:2] + struct.pack('>HHHHH', 0x8180, 1, 1, 0, 0) + question + record


def test_txt_query_and_refresh_store_names(monkeypatch):
    host, port = serve(lambda q: txt_answer(q, '273993 | CO | lacnic | 2021-01-01 | EDGEUNO S.A.S, CO'))
    assert REAL_TXT('AS273993.asn.cymru.com', server=host, port=port) == '273993 | CO | lacnic | 2021-01-01 | EDGEUNO S.A.S, CO'
    monkeypatch.setattr(asnames, 'txt', lambda *a, **k: '7195 | CO | lacnic | 2011-01-01 | EDGEUNO S.A.S, CO')
    with session() as db:
        assert asnames.refresh(db, {7195, None}) == 1;db.commit()
        row = db.get(AutonomousSystem, 7195)
        assert (row.org, row.country) == ('EDGEUNO', 'CO')
        assert asnames.refresh(db, {7195}) == 0
        assert asnames.lookup(db, {7195, 64500}) == {7195: {'org': 'org:EDGEUNO', 'label': 'EdgeUno'}, 64500: {'org': 'as:64500', 'label': 'AS64500'}}


def test_route_names_each_network_once():
    names = {273103: {'label': 'Tv&Mas'}, 7195: {'label': 'EdgeUno'}, 2906: {'label': 'Netflix'}}
    hops = [{'address': '172.20.0.1', 'asn': None}, {'address': '38.0.0.1', 'asn': 273103}, {'address': '200.0.0.1', 'asn': 7195},
            {'address': '200.0.0.2', 'asn': 7195}, {'address': None, 'asn': None}, {'address': '207.0.0.1', 'asn': 2906}]
    assert asnames.route(hops, names) == 'Tv&Mas → EdgeUno → Netflix'
    assert asnames.route(hops[:3], names, reached=False) == 'Tv&Mas → EdgeUno (no llegó al destino)'


def test_latency_case_gets_incident_traces_and_route_changes():
    def path(transit):
        return [{'hop': 1, 'address': '38.191.196.44', 'asn': 273103, 'loss': 0.0}, {'hop': 2, 'address': '200.25.51.1', 'asn': transit, 'loss': 0.0},
                {'hop': 3, 'address': '10.20.0.2', 'asn': 6507, 'loss': 0.0}]
    with session() as db:
        t = target(db);stamp = now()
        incident = ProbeIncident(target_id=t.id, address=t.address, opened_at=stamp - 600, severity='critical', conditions={}, profile={})
        db.add(incident);db.flush()
        case = Case(kind='latency', key='k', entity_key='service:Riot', entity_label='Riot', subject='Riot', detail={'scope': 'external'},
                    opened_at=stamp - 600, last_activity_at=stamp - 600, last_state='down', last_state_at=stamp - 600, rule_version=4)
        db.add(case);db.flush()
        db.add(CaseEvidence(case_id=case.id, source='probe', ref=incident.id, role='trigger', strength='entity', reason='x',
                            occurred_at=stamp - 600, title='x', data={}, rule_version=4))
        trace = lambda **k: ProbeTrace(target_id=t.id, address=t.address, protocol='icmp', status='done', reached=True, path={}, **k)
        db.add_all([trace(reason='baseline', slot=stamp - 2000, finished_at=stamp - 2000, hops=path(7195), fingerprint='a'),
                    trace(reason='baseline', slot=stamp - 900, finished_at=stamp - 900, hops=path(174), fingerprint='b', path_changed=True),
                    trace(reason='incident_opened', slot=stamp - 590, finished_at=stamp - 590, hops=path(174), fingerprint='b',
                          incident_id=incident.id),
                    # Mixed-up replies are never shown as a path.
                    trace(reason='baseline', slot=stamp - 300, finished_at=stamp - 300, fingerprint='c', path_changed=True,
                          hops=path(174) + [{'hop': 4, 'address': '38.191.196.44', 'asn': 273103, 'loss': 0.0}])])
        db.commit()
        found = correlator.traces(db, case, Entity(key='service:Riot', label='Riot', device_id=None), stamp - 3600, stamp)
        assert [e.title for e in found] == [
            'PE-LAB: cambio de ruta · antes AS273103 → EdgeUno → Riot · ahora AS273103 → Cogent → Riot',
            'PE-LAB: traza al abrirse el incidente · AS273103 → Cogent → Riot']
        assert {(e.source, e.role) for e in found} == {('trace', 'context')}
