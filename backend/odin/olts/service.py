"""SmartOLT reader on the job queue: outages every 5 minutes, OLT inventory every hour, ONUs once a day.

Each outage read also keeps, per PON, how many ONUs are down by cause (PonSample): a row when it changes and one an
hour otherwise, for 90 days, so a PON's normal can be learned by time of day.

Each read runs as one job of the worker. Network reads happen with no database transaction open; the result is
written afterwards in one transaction. Due times carry a random delay, as SmartOLT asks, and a 429 stops every read
until its Retry-After. With 2 OLTs a day costs about 30 calls per hour of the 1,000 the account has.
Timestamps from SmartOLT are local to the account: they are read in the zone set for the source (UTC by default).
"""
import datetime as dt
import ipaddress
import logging
import random
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from collections import Counter
from sqlalchemy import delete, func, select, update
from ..db import session
from ..jobs import enqueue
from ..models import (Device, Integration, InventoryInterface, Job, Olt, OltOutage, OltPonPort, OltUplink, Onu, PonSample, SmartoltState,
                      SourceIdentity, now, uid)
from ..security import decrypt
from ..inventory.connectors import ConnectorError
from .connector import SmartOLT, RateLimited

INTERVALS = {'outages': 300, 'inventory': 3600, 'onus': 86400}
# Inventory first: outages are read per OLT, so they need the OLT list (an account without OLTs costs no outage calls).
ORDER = ('inventory', 'outages', 'onus')
CAUSES = ('los', 'power', 'offline')
SAMPLE_KEEPALIVE = 3600
SAMPLE_RETENTION = 90 * 86400
NUMBER = re.compile(r'-?\d+(?:[.,]\d+)?')
UPTIME = re.compile(r'(?:(\d+)\s*days?,?\s*)?(\d+):(\d{2})(?::(\d{2}))?')


def number(value):
    """A float from SmartOLT's text values ('6.839 dBm', '24°C', '-20.05 dBm'); None for 'N/A', '-' or empty."""
    if isinstance(value, bool) or value is None:return None
    if isinstance(value, (int, float)):return float(value)
    found = NUMBER.search(str(value))
    return float(found.group().replace(',', '.')) if found else None


def count(value):
    try:return max(0, int(value))
    except (TypeError, ValueError):return 0


def uptime(value):
    found = UPTIME.search(str(value or ''))
    if not found:return None
    days, hours, minutes, seconds = (int(x or 0) for x in found.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def stamp(value, zone):
    try:return int(dt.datetime.strptime(str(value), '%Y-%m-%d %H:%M:%S').replace(tzinfo=zone).timestamp())
    except (TypeError, ValueError):return None


def zone_of(integration):
    try:return ZoneInfo(integration.events_timezone or 'UTC')
    except (ZoneInfoNotFoundError, ValueError):return ZoneInfo('UTC')


def text(value, limit):
    return str(value if value is not None else '')[:limit]


def state(db):
    row = db.get(SmartoltState, 'smartolt')
    if not row:row = SmartoltState(id='smartolt', due={}, tasks={});db.add(row);db.flush()
    return row


def jitter(interval):
    return random.randint(0, max(15, interval // 10))


def schedule(db):
    """Queue the most urgent due read, one at a time."""
    integration = db.get(Integration, 'smartolt')
    if not integration or not integration.events_enabled or not integration.encrypted_secret:return
    row = db.scalar(select(SmartoltState).where(SmartoltState.id == 'smartolt').with_for_update()) or state(db)
    stamp_ = now()
    if row.config_version != integration.version:
        # Another account or key: read everything again now.
        row.config_version, row.due, row.tasks = integration.version, {}, {}
    previous = db.get(Job, row.job_id) if row.job_id else None
    if previous and previous.status in ('pending', 'running'):db.commit();return
    if row.blocked_until and row.blocked_until > stamp_:db.commit();return
    task = next((t for t in ORDER if (row.due or {}).get(t, 0) <= stamp_), None)
    if task:
        row.job_id = enqueue(db, 'smartolt_poll', {'task': task, 'config_version': integration.version}, 'smartolt:' + uid()).id
    db.commit()


def fetch(client, task, known_olts):
    """Network only. Returns what `write` stores; no database access."""
    if task == 'inventory':
        olts = client.get('/api/system/get_olts').get('response') or []
        health = {str(x.get('olt_id')): x for x in client.get('/api/olt/get_olts_uptime_and_env_temperature').get('response') or []
                  if isinstance(x, dict)}
        ports = {}
        for olt in olts:
            key = str(olt.get('id'))
            ports[key] = (client.get('/api/system/get_olt_pon_ports_details/{}', key).get('response') or [],
                          client.get('/api/system/get_olt_uplink_ports_details/{}', key).get('response') or [])
        return {'olts': [x for x in olts if isinstance(x, dict) and x.get('id') is not None], 'health': health, 'ports': ports}
    if task == 'outages':
        return {'outages': {key: client.get('/api/system/get_outage_pons/{}', key).get('response') or {} for key in known_olts}}
    return {'onus': client.onus()}


def device_by_address(db):
    found = {}
    for identity in db.scalars(select(SourceIdentity).where(SourceIdentity.present.is_(True))):
        for value in identity.addresses or []:
            try:found.setdefault(ipaddress.ip_interface(value).ip, identity.device_id)
            except ValueError:continue
    rows = db.execute(select(InventoryInterface.addresses, SourceIdentity.device_id)
                      .join(SourceIdentity, SourceIdentity.id == InventoryInterface.identity_id).where(InventoryInterface.present.is_(True))).all()
    for addresses, device_id in rows:
        for value in addresses or []:
            try:found.setdefault(ipaddress.ip_interface(value).ip, device_id)
            except ValueError:continue
    return found


def write_inventory(db, data, stamp_, zone=None):
    devices = device_by_address(db)
    seen = set()
    for item in data['olts']:
        key = text(item['id'], 40);seen.add(key)
        # Required columns set on creation: looking up the device below flushes the new row.
        olt = db.get(Olt, key) or Olt(id=key, name=key, first_seen=stamp_, last_seen=stamp_, pop_manual=False, reachable=True, present=True)
        db.add(olt)
        olt.name, olt.hardware = text(item.get('name') or key, 200), text(item.get('olt_hardware_version'), 120)
        try:olt.ip = str(ipaddress.ip_address(str(item.get('ip') or '').strip()))
        except ValueError:olt.ip = None
        olt.device_id = devices.get(ipaddress.ip_address(olt.ip)) if olt.ip else None
        if not olt.pop_manual:
            device = db.get(Device, olt.device_id) if olt.device_id else None
            olt.pop_id = device.pop_id if device else olt.pop_id
        health = data['health'].get(key) or {}
        olt.uptime_seconds, olt.temperature = uptime(health.get('uptime')), number(health.get('env_temp'))
        olt.present, olt.last_seen = True, stamp_
        db.flush()
        pons, uplinks = data['ports'].get(key, ([], []))
        for pon in pons:
            if not isinstance(pon, dict):continue
            row = db.get(OltPonPort, (key, text(pon.get('board'), 10), text(pon.get('pon_port'), 10)))
            row = row or OltPonPort(olt_id=key, board=text(pon.get('board'), 10), port=text(pon.get('pon_port'), 10), last_seen=stamp_)
            db.add(row)
            row.pon_type, row.admin_status = text(pon.get('pon_type'), 20), text(pon.get('admin_status'), 30)
            row.status, row.description = text(pon.get('operational_status'), 30), text(pon.get('description'), 300)
            row.tx_power, row.onus, row.online = number(pon.get('tx_power')), count(pon.get('onus_count')), count(pon.get('online_onus_count'))
            # SmartOLT reports 0 as the average of a PON without ONUs: that is no reading.
            average = number(pon.get('average_signal'))
            row.average_signal, row.last_seen = (average if average else None), stamp_
        for link in uplinks:
            if not isinstance(link, dict) or not link.get('name'):continue
            row = db.get(OltUplink, (key, text(link['name'], 80))) or OltUplink(olt_id=key, name=text(link['name'], 80), last_seen=stamp_)
            db.add(row)
            row.description, row.kind = text(link.get('description'), 300), text(link.get('type'), 30)
            row.admin_status, row.status = text(link.get('admin_status'), 30), text(link.get('status'), 40)
            row.negotiation, row.temperature, row.last_seen = text(link.get('negotiation_auto'), 60), number(link.get('temperature')), stamp_
    db.execute(update(Olt).where(Olt.id.not_in(seen or {''})).values(present=False))
    db.execute(OltPonPort.__table__.delete().where(OltPonPort.olt_id.in_(seen or {''}), OltPonPort.last_seen < stamp_))
    db.execute(OltUplink.__table__.delete().where(OltUplink.olt_id.in_(seen or {''}), OltUplink.last_seen < stamp_))


def outage_items(response):
    """(cause, pon) for every PON in the cause sections and in the stale list."""
    for section in response.get('sections') or []:
        cause = section.get('key') if isinstance(section, dict) else None
        if cause not in CAUSES:continue
        for group in section.get('groups') or []:
            for pon in (group.get('pons') or []) if isinstance(group, dict) else []:
                if isinstance(pon, dict):yield cause, pon
    for group in (response.get('stale') or {}).get('groups') or []:
        for pon in (group.get('pons') or []) if isinstance(group, dict) else []:
            if isinstance(pon, dict):yield 'stale', pon


def write_outages(db, data, stamp_, zone):
    for key, response in data['outages'].items():
        olt = db.get(Olt, key)
        if not olt:continue
        unreachable = {str(x.get('olt_id') or x.get('id')) if isinstance(x, dict) else str(x) for x in response.get('unreachable_olts') or []}
        olt.reachable = key not in unreachable
        active = {(o.board, o.port, o.cause): o for o in db.scalars(select(OltOutage).where(OltOutage.olt_id == key, OltOutage.active.is_(True)))}
        current = set()
        for cause, pon in outage_items(response):
            if str(pon.get('olt_id', key)) != key:continue
            ident = (text(pon.get('board'), 10), text(pon.get('port'), 10), cause)
            if ident in current:continue
            current.add(ident)
            row = active.get(ident)
            if not row:
                row = OltOutage(id=uid(), olt_id=key, board=ident[0], port=ident[1], cause=cause, active=True, first_seen=stamp_, last_seen=stamp_)
                db.add(row)
            affected = {'los': 'los_count', 'power': 'power_count', 'offline': 'offline_count'}.get(cause)
            row.zone, row.description = text(pon.get('zone_name'), 120), text(pon.get('pon_description'), 300)
            row.started_at = stamp(pon.get('latest_status_change'), zone) or row.started_at
            row.subscribers = count(pon.get('total_onus'))
            row.affected = count(pon.get(affected)) if affected else count(pon.get('total_onus'))
            row.odbs = [x.strip() for x in str(pon.get('odb_names') or '').split(',') if x.strip()][:64]
            row.last_seen = stamp_
        for ident, row in active.items():
            if ident not in current:row.active, row.ended_at = False, stamp_
        sample_pons(db, key, stamp_)
    db.execute(delete(PonSample).where(PonSample.at < stamp_ - SAMPLE_RETENTION))


def sample_pons(db, olt_id, stamp_):
    """A row per PON with ONUs when what is down changed since its last row, or SAMPLE_KEEPALIVE after it."""
    down = {}
    for o in db.scalars(select(OltOutage).where(OltOutage.olt_id == olt_id, OltOutage.active.is_(True), OltOutage.cause.in_(CAUSES))):
        down.setdefault((o.board, o.port), Counter())[o.cause] += o.affected
    latest = (select(PonSample.board, PonSample.port, func.max(PonSample.at).label('at')).where(PonSample.olt_id == olt_id)
              .group_by(PonSample.board, PonSample.port).subquery())
    last = {(r.board, r.port): r for r in db.scalars(select(PonSample).join(latest, (PonSample.board == latest.c.board)
                                                       & (PonSample.port == latest.c.port) & (PonSample.at == latest.c.at)).where(PonSample.olt_id == olt_id))}
    for pon in db.scalars(select(OltPonPort).where(OltPonPort.olt_id == olt_id, OltPonPort.onus > 0)):
        counts = down.get((pon.board, pon.port), Counter())
        values = (pon.onus, counts['los'], counts['power'], counts['offline'])
        before = last.get((pon.board, pon.port))
        if before and (before.onus, before.los, before.power, before.offline) == values and stamp_ - before.at < SAMPLE_KEEPALIVE:continue
        if before and before.at == stamp_:continue
        db.add(PonSample(olt_id=olt_id, board=pon.board, port=pon.port, at=stamp_, onus=values[0], los=values[1], power=values[2], offline=values[3]))


def write_onus(db, data, stamp_, zone):
    seen = set()
    for item in data['onus']:
        key = text(item.get('unique_external_id'), 120)
        if not key or key in seen:continue
        seen.add(key)
        row = db.get(Onu, key) or Onu(id=key, olt_id='', present=True, last_seen=stamp_)
        db.add(row)
        row.sn, row.olt_id = text(item.get('sn'), 64), text(item.get('olt_id'), 40)
        row.board, row.port, row.onu = text(item.get('board'), 10), text(item.get('port'), 10), text(item.get('onu'), 10)
        row.onu_type, row.zone, row.odb = text(item.get('onu_type_name'), 120), text(item.get('zone_name'), 120), text(item.get('odb_name'), 120)
        row.name, row.admin_status = text(item.get('name'), 200), text(item.get('administrative_status'), 30)
        row.status, row.status_changed_at = text(item.get('status'), 30), stamp(item.get('last_status_change'), zone)
        row.signal, row.signal_1310, row.signal_1490 = text(item.get('signal'), 30), number(item.get('signal_1310')), number(item.get('signal_1490'))
        row.present, row.last_seen = True, stamp_
    db.execute(update(Onu).where(Onu.id.not_in(seen or {''})).values(present=False))


def execute(job_id, token):
    with session() as db:
        job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if not job or job.status != 'running' or job.lease_token != token or job.lease_until <= now():return
        integration = db.get(Integration, 'smartolt')
        task = job.payload.get('task')
        if (not integration or integration.version != job.payload.get('config_version') or not integration.encrypted_secret
                or task not in INTERVALS):
            job.status = 'done';job.lease_token = None;job.lease_until = None;job.result = {'status': 'cancelled'};db.commit();return
        endpoint, secret, zone = integration.endpoint, decrypt(integration.encrypted_secret), zone_of(integration)
        known = [o.id for o in db.scalars(select(Olt).where(Olt.present.is_(True)).order_by(Olt.id))]
        db.commit()
    error, retry_after, client, data = None, None, None, None
    try:
        client = SmartOLT(endpoint, secret)
        data = fetch(client, task, known)
    except RateLimited as exc:
        error, retry_after = exc.code, exc.retry_after
    except ConnectorError as exc:
        error = exc.code
    except Exception:
        logging.exception('smartolt_read_failed');error = 'invalid_response'
    with session() as db:
        job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if not job or job.lease_token != token:return
        row = db.scalar(select(SmartoltState).where(SmartoltState.id == 'smartolt').with_for_update()) or state(db)
        stamp_ = now()
        tasks, due = dict(row.tasks or {}), dict(row.due or {})
        info = dict(tasks.get(task) or {})
        if not error:
            try:
                with db.begin_nested():
                    {'inventory': write_inventory, 'outages': write_outages, 'onus': write_onus}[task](db, data, stamp_, zone)
            except Exception:
                logging.exception('smartolt_write_failed');error = 'write_failed'
        if error:
            info['failures'] = info.get('failures', 0) + 1
            info['last_error'], info['last_failure_at'] = error, stamp_
            due[task] = stamp_ + min(INTERVALS[task], 60 * 2 ** min(info['failures'], 6)) + jitter(60)
        else:
            info.update(failures=0, last_error=None, last_success_at=stamp_, calls=client.calls)
            due[task] = stamp_ + INTERVALS[task] + jitter(INTERVALS[task])
        if retry_after:row.blocked_until = stamp_ + retry_after
        if client and client.remaining is not None:
            row.rate_remaining, row.rate_limit, row.rate_observed_at = client.remaining, client.limit, stamp_
        tasks[task] = info;row.tasks, row.due = tasks, due
        job.status = 'done';job.result = {'status': 'failed' if error else 'done', 'error': error, 'task': task}
        job.lease_token = None;job.lease_until = None
        db.commit()
