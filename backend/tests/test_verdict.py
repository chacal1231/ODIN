import hashlib
from types import SimpleNamespace
from odin.db import session
from odin.models import ProbeTarget, ProbeTrace, now, uid
from odin.probes import verdict
from odin.probes.config import ProbeConfig, DEFAULT_VERSION_ID
from odin.cases.summary import group

OWN = 273103


def hops(*asns, end='203.0.113.9'):
    """Docker gateway, the ISP's router, then one hop per AS; the last one is the destination."""
    rows = [{'hop': 1, 'address': '172.20.0.1', 'asn': None, 'loss': 0.0}, {'hop': 2, 'address': '38.191.196.44', 'asn': OWN, 'loss': 0.0}]
    for n, asn in enumerate(asns, 3):
        rows.append({'hop': n, 'address': end if n == len(asns) + 2 else f'198.51.100.{n}', 'asn': asn, 'loss': 0.0})
    return rows


class Lab:
    def __init__(self, db):
        self.db, self.views, self.stamp = db, [], now()

    def check(self, name, service, state, path=None, asn=None, previous=None, age=600):
        t = ProbeTarget(id=uid(), name=name, address='203.0.113.9', family='ipv4', profile_version_id=DEFAULT_VERSION_ID, approved_ips=[],
                        effective=ProbeConfig().model_dump(), enabled=True, service=service, categories=['streaming'],
                        catalog_metadata={'asn_reference': asn} if asn else {})
        self.db.add(t);self.db.flush()
        for offset, value in ((age + 900, previous), (age, path)):
            if value:
                self.db.add(ProbeTrace(target_id=t.id, address=t.address, protocol='icmp', reason='baseline', slot=self.stamp - offset,
                                       status='done', finished_at=self.stamp - offset, reached=True, hops=value, path={},
                                       fingerprint=hashlib.sha256(str([h['asn'] for h in value]).encode()).hexdigest()[:32]))
        self.db.commit()
        self.views.append({'id': t.id, 'name': name, 'service': service, 'state': state, 'enabled': True, 'archived': False,
                           'categories': ['streaming'], 'catalog_metadata': t.catalog_metadata})

    def run(self):
        return verdict.compute(self.db, self.views, self.stamp)


def test_degradation_concentrated_on_one_exit_blames_it():
    with session() as db:
        lab = Lab(db)
        lab.check('A', 'Netflix', 'critical', hops(7195, 2906), 2906)
        lab.check('B', 'Valve', 'warning', hops(7195, 32590), 32590)
        lab.check('C', 'Spotify', 'healthy', hops(7195, 54113), 54113)
        lab.check('D', 'Teams', 'healthy', hops(174, 8075), 8075)
        result = lab.run()
        assert result['own_asn'] == OWN and result['state'] == 'exit'
        assert result['findings'][0]['exit'] == 'EdgeUno' and result['findings'][0]['services'] == ['Netflix', 'Valve']
        assert result['title'] == 'Falla en una salida: 2 de 3 servicios que salen por EdgeUno están degradados'
        assert 'Abrir ticket con EdgeUno' in result['action']


def test_one_service_failing_through_a_healthy_exit_is_the_service():
    with session() as db:
        lab = Lab(db)
        lab.check('A', 'Netflix', 'critical', hops(7195, 2906), 2906)
        lab.check('B', 'Valve', 'healthy', hops(7195, 32590), 32590)
        lab.check('C', 'Spotify', 'healthy', hops(7195, 54113), 54113)
        result = lab.run()
        assert result['state'] == 'service' and result['findings'][0]['service'] == 'Netflix'
        assert '2 servicios más por la misma salida (EdgeUno) están normales' in result['title']
        assert result['action'].startswith('No es la red')


def test_several_exits_at_once_is_our_own_network():
    with session() as db:
        lab = Lab(db)
        lab.check('A', 'Netflix', 'critical', hops(7195, 2906), 2906)
        lab.check('B', 'Valve', 'critical', hops(7195, 32590), 32590)
        lab.check('C', 'Teams', 'critical', hops(174, 8075), 8075)
        lab.check('D', 'Zoom', 'critical', hops(174, 209242), 209242)
        lab.check('E', 'Spotify', 'healthy', hops(174, 54113), 54113)
        result = lab.run()
        assert result['state'] == 'own' and len(result['findings']) == 1
        assert result['title'] == 'Problema propio o del origen: 4 de 5 servicios degradados por 2 salidas distintas'


def test_direct_interconnection_and_undetermined_and_route_change():
    with session() as db:
        lab = Lab(db)
        # Google services peer directly: all of them failing points to Google or the link to it.
        lab.check('YT', 'YouTube', 'critical', hops(15169), 15169)
        lab.check('GM', 'Gmail', 'critical', hops(15169), 15169)
        lab.check('X', 'Valve', 'healthy', hops(7195, 32590), 32590)
        # No trace: nothing to compare.
        lab.check('N', 'Nequi', 'warning')
        # Left EdgeUno for Cogent while degraded.
        lab.check('R', 'Riot', 'critical', hops(174, 6507), 6507, previous=hops(7195, 6507))
        lab.check('R2', 'Steam', 'healthy', hops(174, 32590), 32590)
        # Enough healthy services that the degraded ones are not half of everything.
        for name, path in (('Spotify', hops(7195, 54113)), ('Teams', hops(174, 8075)), ('Zoom', hops(174, 209242))):
            lab.check(name, name, 'healthy', path)
        result = lab.run()
        states = {f.get('exit') or f.get('service'): f for f in result['findings']}
        assert states['Google']['state'] == 'exit' and states['Google']['direct']
        assert 'Revisar la interconexión directa con Google' in states['Google']['action']
        assert states['Nequi']['state'] == 'undetermined' and 'sin traza' in states['Nequi']['title']
        assert states['Cogent']['state'] == 'service' and states['Cogent']['route_changes']['Riot']['before'] == 'EdgeUno'


def test_mixed_up_and_old_traces_are_not_read():
    with session() as db:
        lab = Lab(db)
        mixed = hops(7195, 2906);mixed[3]['address'] = '38.191.196.44'
        lab.check('A', 'Netflix', 'critical', mixed, 2906)
        lab.check('B', 'Valve', 'critical', hops(7195, 32590), 32590, age=3 * 3600)
        result = lab.run()
        assert {f['state'] for f in result['findings']} == {'undetermined'}
        assert verdict.compute(db, [], origin_healthy=False)['state'] == 'origin'


def test_external_case_alerts_by_verdict():
    case = lambda state: SimpleNamespace(kind='latency', severity='warning', detail={'scope': 'external', 'verdict': {'state': state} if state else None})
    assert group(case('exit'), 1) == group(case('own'), 1) == 'external'
    assert group(case('service'), 5) is None
    assert group(case(None), 3) == 'external' and group(case('undetermined'), 2) is None
