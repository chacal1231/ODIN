"""Traceroutes (mtr) towards monitored destinations, each hop read against the inventory.

mtr runs from the ODIN server in its own process (`tracer`): a trace takes seconds and must not delay the probe
rounds, which are dropped when they start late.
When: a baseline every 15 minutes per enabled check, one when an incident opens, one when it recovers, and on demand.
Address: always one the check already measures (approved or resolved); hostnames never reach mtr.
Hops: an address of an own port (or of an inventory device) is that router; an address inside the subnet of an own
port is the far end of that link, with the role of the port description (transit, IXP, peering...), see roles.py.
ASN per hop comes from mtr (Team Cymru over DNS): the origin AS of the covering prefix, not the operator of the router.
The path fingerprint keeps routers, hand-off links and autonomous systems, not every address: equal-cost paths inside
one network are not a change. Only traces that reached the destination are compared, or, for a catalog check with a
reference ASN, traces whose last reply came from inside that network: game servers and hosts that drop probes
(and a destination down during an incident) still show whether the way into their network changed.
Limits: one vantage point, the return path is not seen; a provider's MPLS core without TTL propagation shows fewer
hops; hops without reply are normal (many routers do not answer or rate-limit ICMP).
"""
import hashlib
import ipaddress
import json
import logging
import math
import os
import signal
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import select, update, text, or_
from sqlalchemy.dialects.postgresql import insert
from ..config import settings
from ..db import session
from ..models import ProbeTarget, ProbeTrace, ProbeOrigin, SourceIdentity, Device, now, uid
from ..inventory import roles
from .config import safe_ip
from .resolver import ProbeError
from . import asnames

INTERVAL = 900
CYCLES = 10
MAX_HOPS = 30
TIMEOUT = 90
LEASE = 120
REASONS = ('baseline', 'incident_opened', 'incident_recovered', 'manual')
ORIGIN = 'tracer'


def slot_for(target_id, stamp):
    offset = int(target_id.replace('-', '')[:8], 16) % INTERVAL
    return stamp - (stamp - offset) % INTERVAL


def candidates(target):
    return list(target.resolved_ips or target.approved_ips or [])


def request(db, target, reason, slot, incident_id=None, actor_id=None, preferred=None):
    """Queue a trace towards an address the check measures; None when it has none yet (DNS not resolved)."""
    addresses = candidates(target)
    if not addresses or target.archived:return None
    address = preferred if preferred in addresses else sorted(addresses)[0]
    protocol = 'icmp' if (target.protocol or 'icmp') == 'icmp' else 'tcp'
    port = (target.check_config or {}).get('port', 443) if protocol == 'tcp' else None
    values = dict(id=uid(), target_id=target.id, address=address, protocol=protocol, port=port, reason=reason,
                  incident_id=incident_id, actor_id=actor_id, slot=slot, status='pending', hops=[], path={})
    statement = insert(ProbeTrace).values(**values)
    if reason == 'baseline':
        statement = statement.on_conflict_do_nothing(index_elements=['target_id', 'slot'], index_where=text("reason = 'baseline'"))
    db.execute(statement)
    return values['id']


def schedule(db, stamp):
    """Baselines for the current 15-minute slot of each enabled check; a slot missed by more than two minutes is not replayed."""
    for target in db.scalars(select(ProbeTarget).where(ProbeTarget.enabled.is_(True), ProbeTarget.archived.is_(False))):
        slot = slot_for(target.id, stamp)
        if stamp - slot < 120:request(db, target, 'baseline', slot)
    db.commit()


def claim(db, stamp=None):
    stamp = stamp or now()
    db.execute(update(ProbeTrace).where(ProbeTrace.status == 'running', ProbeTrace.lease_until < stamp)
               .values(status='failed', error_code='trace_interrupted', finished_at=stamp, token=None))
    # A late baseline is replaced by the next one; incident and manual traces wait their turn.
    db.execute(update(ProbeTrace).where(ProbeTrace.status == 'pending', ProbeTrace.reason == 'baseline', ProbeTrace.slot < stamp - INTERVAL)
               .values(status='cancelled', error_code='trace_skipped', finished_at=stamp))
    db.commit()
    row = db.scalar(select(ProbeTrace).where(ProbeTrace.status == 'pending')
                    .order_by(ProbeTrace.reason == 'baseline', ProbeTrace.slot).with_for_update(skip_locked=True).limit(1))
    if not row:db.commit();return None
    target = db.get(ProbeTarget, row.target_id)
    if not target or target.archived or (row.reason == 'baseline' and not target.enabled):
        row.status = 'cancelled';row.error_code = 'configuration_changed';row.finished_at = stamp;db.commit();return {'skipped': True}
    if row.address not in candidates(target):
        row.status = 'cancelled';row.error_code = 'trace_address_changed';row.finished_at = stamp;db.commit();return {'skipped': True}
    row.status = 'running';row.token = uid();row.lease_until = stamp + LEASE
    db.commit()
    return {'id': row.id, 'token': row.token, 'address': row.address, 'family': target.family,
            'protocol': row.protocol, 'port': row.port}


def version():
    try:
        result = subprocess.run(['mtr', '--version'], capture_output=True, text=True, timeout=3, check=False)
        value = (result.stdout + result.stderr).strip()
        if result.returncode or not value.startswith('mtr 0.95'):raise ProbeError('mtr_version_unsupported')
        return value[:80]
    except (OSError, subprocess.TimeoutExpired):raise ProbeError('mtr_unavailable')


def command(address, family, protocol, port):
    result = ['mtr', '--json', '-n', '-z', '-4' if family == 'ipv4' else '-6', '-c', str(CYCLES), '-i', '0.5', '-G', '2', '-m', str(MAX_HOPS)]
    if protocol == 'tcp':result += ['--tcp', '--port', str(int(port))]
    return result + ['--', address]


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ProbeError('trace_parse_failed')
    return float(value)


def consistent(hops):
    """One address at two hops apart is a reply credited to the wrong probe (or a loop), not a path: never compare it."""
    positions = {}
    for index, hop in enumerate(h for h in hops if h['address']):positions.setdefault(hop['address'], []).append(index)
    return all(p[-1] - p[0] + 1 == len(p) for p in positions.values())


def parse(output, address):
    """Hops up to the destination; a hop without reply has no address and no times."""
    try:hubs = json.loads(output)['report']['hubs']
    except (ValueError, KeyError, TypeError):raise ProbeError('trace_parse_failed')
    if not isinstance(hubs, list) or len(hubs) > MAX_HOPS:raise ProbeError('trace_parse_failed')
    hops = []
    for position, hub in enumerate(hubs, 1):
        try:
            if hub['count'] != position:raise ProbeError('trace_parse_failed')
            host = None if hub['host'] == '???' else str(ipaddress.ip_address(hub['host']))
            loss, sent = number(hub['Loss%']), int(hub['Snt'])
            asn = hub.get('ASN', '')
            asn = int(asn[2:]) if isinstance(asn, str) and asn[:2] == 'AS' and asn[2:].isdigit() else None
            answered = host is not None and loss < 100
            hops.append({'hop': position, 'address': host, 'asn': asn if host else None, 'loss': min(loss, 100.0), 'sent': sent,
                         **{key: number(hub[name]) if answered else None
                            for key, name in (('avg', 'Avg'), ('best', 'Best'), ('worst', 'Wrst'), ('stdev', 'StDev'))}})
        except (KeyError, TypeError, ValueError):raise ProbeError('trace_parse_failed')
        if host == address:break
    if not consistent(hops):raise ProbeError('trace_inconsistent')
    reached = bool(hops) and hops[-1]['address'] == address and hops[-1]['loss'] < 100
    return hops, reached


def run(address, family, protocol, port):
    address = safe_ip(address, family)
    try:
        result = subprocess.run(command(address, family, protocol, port), capture_output=True, text=True, check=False,
                                timeout=TIMEOUT, env={**os.environ, 'LC_ALL': 'C'})
    except subprocess.TimeoutExpired:raise ProbeError('trace_timeout')
    except OSError:raise ProbeError('mtr_unavailable')
    if result.returncode:raise ProbeError('trace_execution_failed')
    return parse(result.stdout, address)


class Inventory:
    """Own routers by address and the far ends of own ports, from the inventory at the time of the trace."""

    def __init__(self, db):
        self.network = roles.Network(db)
        devices = {d.id: d for d in db.scalars(select(Device).where(Device.archived.is_(False)))}
        self.devices = {}
        for port in self.network.ports:
            self.devices[port.network.ip] = {'device_id': port.device_id, 'device_name': port.device_name, 'pop_id': port.pop_id,
                                             'interface': port.name}
        for identity in db.scalars(select(SourceIdentity).where(SourceIdentity.present.is_(True))):
            device = devices.get(identity.device_id)
            for value in identity.addresses or []:
                try:ip = ipaddress.ip_interface(value).ip
                except ValueError:continue
                if device and ip not in self.devices:
                    self.devices[ip] = {'device_id': device.id, 'device_name': device.name, 'pop_id': device.pop_id, 'interface': None}

    def read(self, address, destination):
        ip = ipaddress.ip_address(address)
        own = self.devices.get(ip)
        found = None if own else self.network.neighbor(address)
        if found and found['remote_device_id']:
            # The far end of an internal link is another own router.
            own = {'device_id': found['remote_device_id'], 'device_name': found['remote_device_name'], 'pop_id': None,
                   'interface': found['remote_interface']}
            found = None
        if own:
            info = {'segment': 'own', **own}
        elif found:
            info = {'segment': 'handoff', 'device_id': found['device_id'], 'device_name': found['device_name'], 'pop_id': found['pop_id'],
                    'interface': found['interface'], 'role': found['role'], 'provider': found['provider'],
                    'label': roles.describe(found)}
        else:
            info = {'segment': 'external' if ip.is_global else 'private'}
        if address == destination:info['segment'] = 'destination'
        return info


def enrich(hops, inventory, destination):
    return [{**hop, **(inventory.read(hop['address'], destination) if hop['address'] else {'segment': 'no_reply'})} for hop in hops]


def summarize(hops):
    """The path in routers, hand-off links and networks; its fingerprint ignores addresses within each one."""
    tokens, asns = [], []
    for hop in hops:
        if hop['segment'] == 'handoff':token = 'port:%s:%s' % (hop['device_id'], hop['interface'])
        elif hop.get('device_id'):token = 'device:' + hop['device_id']
        elif hop['asn']:token = 'as:%d' % hop['asn']
        else:continue
        if not tokens or tokens[-1] != token:tokens.append(token)
        if hop['asn'] and hop['segment'] in ('external', 'destination', 'handoff') and hop['asn'] not in asns:asns.append(hop['asn'])
    own = [h for h in hops if h['segment'] == 'own']
    handoff = next((h for h in hops if h['segment'] == 'handoff'), None)
    path = {'last_own': {k: own[-1][k] for k in ('device_id', 'device_name', 'interface')} if own else None,
            'handoff': {k: handoff[k] for k in ('device_id', 'device_name', 'interface', 'role', 'provider', 'label')} if handoff else None,
            'asns': asns, 'tokens': tokens}
    return path, hashlib.sha256('|'.join(tokens).encode()).hexdigest()[:32] if tokens else None


def execute(task):
    hops, reached, error = [], None, None
    try:hops, reached = run(task['address'], task['family'], task['protocol'], task['port'])
    except ProbeError as exc:error = exc.code
    except Exception:error = 'trace_execution_failed'
    with session() as db:
        row = db.scalar(select(ProbeTrace).where(ProbeTrace.id == task['id']).with_for_update())
        if not row or row.status != 'running' or row.token != task['token']:return
        row.finished_at = now();row.token = None;row.lease_until = None
        if error:
            row.status = 'failed';row.error_code = error;db.commit();return
        row.hops = enrich(hops, Inventory(db), row.address)
        row.reached = reached
        path, row.fingerprint = summarize(row.hops)
        target = db.get(ProbeTarget, row.target_id)
        asn = ((target.catalog_metadata or {}).get('asn_reference') if target else None)
        answered = [h for h in row.hops if h['address'] and h['loss'] < 100]
        path['network_reached'] = not reached and bool(asn) and bool(answered) and answered[-1]['asn'] == asn
        row.path = path
        if (reached or path['network_reached']) and row.fingerprint:
            previous = db.scalar(select(ProbeTrace.fingerprint).where(ProbeTrace.target_id == row.target_id, ProbeTrace.address == row.address,
                ProbeTrace.protocol == row.protocol, ProbeTrace.status == 'done', ProbeTrace.fingerprint.is_not(None),
                or_(ProbeTrace.reached.is_(True), ProbeTrace.path['network_reached'].as_boolean().is_(True)),
                ProbeTrace.id != row.id).order_by(ProbeTrace.finished_at.desc()).limit(1))
            row.path_changed = None if previous is None else previous != row.fingerprint
        row.status = 'done'
        db.commit()
        # Names of the networks crossed, for paths and verdicts people read; never blocks a trace result.
        try:
            asnames.refresh(db, {h['asn'] for h in row.hops if h.get('asn')});db.commit()
        except Exception:
            db.rollback();logging.warning('asn_names_failed')


def heartbeat(error, value):
    with session() as db:
        origin = db.get(ProbeOrigin, ORIGIN)
        if not origin:origin = ProbeOrigin(id=ORIGIN, heartbeat=now());db.add(origin)
        origin.heartbeat = now();origin.error_code = error
        if value:origin.fping_version = value  # the column keeps the tool version of each origin
        db.commit()


stop = threading.Event()


def main():
    logging.basicConfig(level=logging.INFO)
    signal.signal(signal.SIGTERM, lambda *_: stop.set());signal.signal(signal.SIGINT, lambda *_: stop.set())
    with ThreadPoolExecutor(max_workers=settings().trace_concurrency) as pool:
        futures, scheduled = set(), 0
        while not stop.is_set():
            try:
                heartbeat(None, version())
                if now() - scheduled >= 30:
                    with session() as db:schedule(db, now())
                    scheduled = now()
                finished = {f for f in futures if f.done()}
                for future in finished:
                    try:future.result()
                    except Exception:logging.error('trace_result_persistence_failed')
                futures -= finished
                while len(futures) < settings().trace_concurrency:
                    with session() as db:task = claim(db)
                    if not task:break
                    if not task.get('skipped'):futures.add(pool.submit(execute, task))
            except ProbeError as exc:
                logging.error('%s', exc.code)
                try:heartbeat(exc.code, None)
                except Exception:pass
            except Exception:
                logging.error('trace_cycle_failed')
                try:heartbeat('trace_cycle_failed', None)
                except Exception:pass
            stop.wait(1)


if __name__ == '__main__':main()
