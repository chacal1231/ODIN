from collections import defaultdict
from itertools import combinations
from sqlalchemy import select, update, text, or_
from ..db import session
from ..models import (Integration, Device, SourceIdentity, InventoryInterface, Candidate, SyncRun, Job, now)
from ..jobs import enqueue, heartbeat
from ..security import decrypt, audit
from .connectors import connector, ConnectorError


def lock_inventory(db):
    db.execute(text('SELECT pg_advisory_xact_lock(712002)'))


def suggest_candidates(db):
    active={d.id for d in db.scalars(select(Device).where(Device.archived.is_(False)))}
    identities=list(db.scalars(select(SourceIdentity).where(SourceIdentity.present.is_(True))))
    index=defaultdict(list)
    for identity in identities:
        if identity.device_id not in active:
            continue
        for address in identity.addresses:
            index[('ip',address)].append(identity)
        names={value.strip().casefold().rstrip('.') for value in (identity.name,identity.details.get('hostname','')) if value}
        for name in names:
            index[('name',name)].append(identity)
        serial=identity.details.get('serial','').strip()
        if serial and serial.lower() not in ('unknown','n/a','none','0'):
            index[('serial',serial)].append(identity)
    suggestions=defaultdict(set)
    for (kind,value),matches in index.items():
        # Avoid quadratic explosions from generic names or shared addresses.
        if len(matches)>20:
            continue
        for a,b in combinations(matches,2):
            if a.source==b.source or a.device_id==b.device_id:
                continue
            pair=tuple(sorted((a.device_id,b.device_id)))
            suggestions[pair].add(f'{kind}: {value}')
    existing={(x.left_id,x.right_id):x for x in db.scalars(select(Candidate))}
    for pair,evidence in suggestions.items():
        if pair not in existing:
            db.add(Candidate(left_id=pair[0],right_id=pair[1],evidence=sorted(evidence)))
        elif existing[pair].status=='pending':
            existing[pair].evidence=sorted(evidence)
    for pair,row in existing.items():
        if row.status=='pending' and pair not in suggestions:
            row.status='obsolete'
        elif row.status=='obsolete' and pair in suggestions:
            row.status='pending'


def apply_snapshot(db,source,devices,stamp):
    """Called only after a complete snapshot, under inventory lock and job lease."""
    existing={x.external_id:x for x in db.scalars(select(SourceIdentity).where(SourceIdentity.source==source))}
    seen=set()
    for item in devices:
        external=item['external_id']
        if external in seen:
            raise ConnectorError('incomplete_snapshot')
        seen.add(external)
        identity=existing.get(external)
        if not identity:
            device=Device(name=item['name'] or external)
            db.add(device); db.flush()
            identity=SourceIdentity(source=source,external_id=external,device_id=device.id,
                name=item['name'] or external,last_seen=stamp)
            db.add(identity); db.flush()
        identity.name=item['name'] or external
        identity.addresses=item['addresses']
        identity.details=item['details']
        identity.last_seen=stamp
        identity.present=True
        # Canonical name, POP and archive state are never overwritten by source sync.
        interfaces={x.external_id:x for x in db.scalars(select(InventoryInterface).where(InventoryInterface.identity_id==identity.id))}
        observed=set()
        for interface in item['interfaces']:
            key=interface['external_id']
            if key in observed:
                raise ConnectorError('incomplete_snapshot')
            observed.add(key)
            row=interfaces.get(key)
            if row is None:
                row=InventoryInterface(identity_id=identity.id,external_id=key)
                db.add(row)
            row.name=interface['name'];row.kind=interface['kind'];row.addresses=interface['addresses'];row.description=interface.get('description','')
            row.last_seen=stamp;row.present=True
        for key,row in interfaces.items():
            if key not in observed:
                row.present=False
    for key,row in existing.items():
        if key not in seen:
            row.present=False
            db.execute(update(InventoryInterface).where(InventoryInterface.identity_id==row.id).values(present=False))
    db.flush()
    suggest_candidates(db)


def queue_run(db,source,mode,actor=None,key=None):
    integration=db.scalar(select(Integration).where(Integration.kind==source).with_for_update())
    # Any source can test its connection; only inventory sources synchronize.
    if not integration or (mode!='test' and source not in ('zabbix','librenms')):
        raise ValueError('source_not_configured')
    running=db.scalar(select(SyncRun).where(SyncRun.source==source,SyncRun.status.in_(['pending','running','retrying'])))
    if running:
        return running
    run=SyncRun(source=source,mode=mode,config_version=integration.version)
    db.add(run);db.flush()
    job=enqueue(db,'inventory',{'run_id':run.id},key or 'inventory:'+run.id)
    run.job_id=job.id
    audit(db,'inventory_'+mode+'_requested',actor,source)
    return run


def schedule(db):
    for run in db.scalars(select(SyncRun).join(Job, SyncRun.job_id==Job.id).where(SyncRun.status.in_(['running','retrying','pending']), Job.status=='failed')):
        run.status='failed';run.error_code='attempts_exhausted';run.finished_at=now()
    for integration in db.scalars(select(Integration).where(Integration.auto_sync.is_(True),Integration.checked_at.is_not(None))):
        if integration.kind not in ('zabbix','librenms'):
            continue
        latest=db.scalar(select(SyncRun).where(SyncRun.source==integration.kind,SyncRun.mode=='sync').order_by(SyncRun.created_at.desc()).limit(1))
        if not latest or latest.created_at+900<=now():
            queue_run(db,integration.kind,'sync')
    db.commit()


def execute(job_id,lease_token):
    with session() as db:
        job=db.scalar(select(Job).where(Job.id==job_id).with_for_update())
        if job.status!='running' or job.lease_token!=lease_token or job.lease_until<=now():
            return
        run=db.get(SyncRun,job.payload['run_id'])
        integration=db.get(Integration,run.source)
        if run.status in ('done','cancelled'):
            job.status='done';job.lease_token=None;db.commit();return
        source,mode,version=run.source,run.mode,run.config_version
        if integration.version!=version:
            run.status='cancelled';run.error_code='configuration_changed';run.finished_at=now()
            job.status='done';job.lease_token=None;db.commit();return
        endpoint,secret,networks,model=integration.endpoint,decrypt(integration.encrypted_secret) if integration.encrypted_secret else '',integration.allowed_networks,integration.model
        run.status='running';db.commit()
    def renew():
        with session() as db:
            if not heartbeat(db,job_id,lease_token):
                raise ConnectorError('lease_expired')
    result=None;error=None
    try:
        adapter=connector(source,endpoint,secret,networks,renew,model)
        result=adapter.test() if mode=='test' else adapter.fetch()
    except ConnectorError as exc:
        error=exc.code
    except Exception:
        error='invalid_response'
    with session() as db:
        lock_inventory(db)
        job=db.scalar(select(Job).where(Job.id==job_id).with_for_update())
        if job.status!='running' or job.lease_token!=lease_token or job.lease_until<=now():
            return
        run=db.get(SyncRun,job.payload['run_id'])
        integration=db.scalar(select(Integration).where(Integration.kind==source).with_for_update())
        if integration.version!=version:
            run.status='cancelled';run.error_code='configuration_changed'
        else:
            if not error and mode=='sync':
                try:
                    with db.begin_nested():
                        apply_snapshot(db,source,result,now())
                except ConnectorError as exc:
                    error=exc.code
            if error:
                run.status='retrying' if job.attempts<4 else 'failed'
                run.error_code=error;integration.last_error=error
            else:
                if mode=='sync':
                    integration.synced_at=now()
                    run.count=len(result)
                integration.checked_at=now();integration.last_error=None
                run.status='done';run.error_code=None
                audit(db,'inventory_'+mode+'_completed',target=source)
        run.finished_at=now()
        retry=run.status=='retrying'
        job.status='pending' if retry else ('failed' if run.status=='failed' else 'done')
        job.run_after=now()+min(120,2**job.attempts)
        job.lease_token=None;job.lease_until=None
        job.result={'run_id':run.id,'status':run.status,'error_code':run.error_code}
        db.commit()


def integration_view(row):
    if not row:
        return {'status':'not_configured','checked_at':None,'synced_at':None,'last_error':None}
    if row.last_error:
        status='error'
    elif row.synced_at:
        status='synced' if row.synced_at+1800>=now() else 'stale'
    elif row.checked_at:
        status='connected'
    else:
        status='configured_not_validated'
    return {'status':status,'checked_at':row.checked_at,'synced_at':row.synced_at,'last_error':row.last_error}


def identity_suggestions(db, entries, source):
    """Inventory devices matching each (name, address) by exact name or by an address another source reported.

    Suggestions only: a hostname or a sending address does not prove identity, an administrator confirms.
    """
    devices = {d.id: d for d in db.scalars(select(Device).where(Device.archived.is_(False)))}
    by_address, by_name = {}, {}
    for identity in db.scalars(select(SourceIdentity).where(SourceIdentity.source != source)):
        for address in identity.addresses:
            by_address.setdefault(address, set()).add(identity.device_id)
    for device in devices.values():
        by_name.setdefault(device.name.casefold(), set()).add(device.id)
    assigned = {i.external_id: devices.get(i.device_id) for i in db.scalars(select(SourceIdentity).where(SourceIdentity.source == source))}
    result = {}
    for name, address in entries:
        found = {}
        for device_id in by_address.get(address, ()):found.setdefault(device_id, []).append('address')
        for device_id in by_name.get(name.casefold(), ()):found.setdefault(device_id, []).append('name')
        device = assigned.get(name)
        result[name] = {'device_id': device.id if device else None, 'device_name': device.name if device else None,
                        'suggestions': [{'device_id': k, 'name': devices[k].name, 'reasons': v} for k, v in found.items() if k in devices]}
    return result


def confirm_identity(db, source, name, address, last_seen, device_id, actor):
    """Create, move or remove the confirmed identity of an external source name. Caller holds the inventory lock."""
    identity = db.scalar(select(SourceIdentity).where(SourceIdentity.source == source, SourceIdentity.external_id == name).with_for_update())
    if device_id:
        device = db.get(Device, device_id)
        if not device or device.archived:
            raise ValueError('Equipo no disponible')
        if not identity:
            identity = SourceIdentity(source=source, external_id=name, device_id=device.id, name=name, addresses=[], details={},
                                      last_seen=last_seen, present=True)
            db.add(identity)
        identity.device_id = device.id;identity.addresses = [address];identity.last_seen = last_seen
        identity.details = {'confirmed_by': actor, 'confirmed_at': now()}
    elif identity:
        db.delete(identity)
    db.flush()

