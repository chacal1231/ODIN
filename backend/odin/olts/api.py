from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, func, case
from sqlalchemy.orm import Session
from ..db import get_db
from ..models import Device, Integration, Olt, OltOutage, OltPonPort, OltUplink, Onu, Pop, now
from ..security import audit, rate_limit
from .service import state, INTERVALS


class SettingsBody(BaseModel):
    version: int = Field(ge=1)
    enabled: bool
    timezone: str | None = Field(default=None, max_length=80)


class OltBody(BaseModel):
    pop_id: str | None = None  # None: back to the POP of its inventory device


def down(admin_status, status):
    """An enabled port that is not up; a disabled one is not a fault."""
    return admin_status.lower() == 'enabled' and status.lower() not in ('up', '') and not status[:1].isdigit()


def source_view(db):
    integration = db.get(Integration, 'smartolt');row = state(db)
    return {'configured': bool(integration and integration.encrypted_secret), 'enabled': bool(integration and integration.events_enabled),
            'version': integration.version if integration else None, 'timezone': integration.events_timezone if integration else None,
            'tasks': row.tasks or {}, 'due': row.due or {}, 'blocked_until': row.blocked_until, 'rate_remaining': row.rate_remaining,
            'rate_limit': row.rate_limit, 'rate_observed_at': row.rate_observed_at, 'intervals': INTERVALS}


def pon_key(row):
    return tuple(int(x) if x.isdigit() else 0 for x in (row.board, row.port))


def olt_view(olt, pons, uplinks, outages, names):
    return {'id': olt.id, 'name': olt.name, 'hardware': olt.hardware, 'ip': olt.ip, 'device_id': olt.device_id,
            'device_name': names['devices'].get(olt.device_id), 'pop_id': olt.pop_id, 'pop_name': names['pops'].get(olt.pop_id),
            'pop_manual': olt.pop_manual, 'uptime_seconds': olt.uptime_seconds, 'temperature': olt.temperature, 'reachable': olt.reachable,
            'present': olt.present, 'last_seen': olt.last_seen, 'pons': len(pons), 'pons_down': sum(down(p.admin_status, p.status) for p in pons),
            'onus': sum(p.onus for p in pons), 'online': sum(p.online for p in pons), 'uplinks': len(uplinks),
            'uplinks_down': sum(down(u.admin_status, u.status) for u in uplinks),
            'outages': sum(o.cause != 'stale' for o in outages), 'affected': sum(o.affected for o in outages if o.cause != 'stale')}


def names(db, olts):
    devices = {o.device_id for o in olts if o.device_id};pops = {o.pop_id for o in olts if o.pop_id}
    return {'devices': dict(db.execute(select(Device.id, Device.name).where(Device.id.in_(devices))).all()) if devices else {},
            'pops': dict(db.execute(select(Pop.id, Pop.name).where(Pop.id.in_(pops))).all()) if pops else {}}


def outage_view(row):
    return {key: getattr(row, key) for key in ('id', 'board', 'port', 'cause', 'zone', 'description', 'started_at', 'subscribers', 'affected',
                                               'odbs', 'active', 'first_seen', 'last_seen', 'ended_at')}


def router(current, admin):
    api = APIRouter(prefix='/api/v1')

    @api.get('/olts')
    def olts(user=Depends(current), db: Session = Depends(get_db)):
        rows = list(db.scalars(select(Olt).order_by(Olt.present.desc(), Olt.name)))
        ids = [o.id for o in rows]
        def group(model, *where):
            found = {k: [] for k in ids}
            for item in db.scalars(select(model).where(model.olt_id.in_(ids), *where)):found[item.olt_id].append(item)
            return found
        pons, uplinks = group(OltPonPort), group(OltUplink)
        outages = group(OltOutage, OltOutage.active.is_(True))
        known = names(db, rows)
        result = {'source': source_view(db), 'olts': [olt_view(o, pons[o.id], uplinks[o.id], outages[o.id], known) for o in rows]}
        db.commit()
        return result

    @api.get('/olts/{olt_id}')
    def olt(olt_id: str, user=Depends(current), db: Session = Depends(get_db)):
        row = db.get(Olt, olt_id)
        if not row:raise HTTPException(404, 'OLT no encontrada')
        pons = sorted(db.scalars(select(OltPonPort).where(OltPonPort.olt_id == olt_id)), key=pon_key)
        uplinks = list(db.scalars(select(OltUplink).where(OltUplink.olt_id == olt_id).order_by(OltUplink.name)))
        active = list(db.scalars(select(OltOutage).where(OltOutage.olt_id == olt_id, OltOutage.active.is_(True)).order_by(OltOutage.started_at)))
        ended = list(db.scalars(select(OltOutage).where(OltOutage.olt_id == olt_id, OltOutage.active.is_(False))
                                .order_by(OltOutage.ended_at.desc()).limit(20)))
        # Signal classes per PON, from the last daily ONU read (SmartOLT's own Critical / Warning classification).
        signals = {(b, p): {'critical': c, 'warning': w} for b, p, c, w in db.execute(
            select(Onu.board, Onu.port, func.sum(case((Onu.signal == 'Critical', 1), else_=0)), func.sum(case((Onu.signal == 'Warning', 1), else_=0)))
            .where(Onu.olt_id == olt_id, Onu.present.is_(True)).group_by(Onu.board, Onu.port)).all()}
        view = olt_view(row, pons, uplinks, active, names(db, [row]))
        view['pon_ports'] = [{**{k: getattr(p, k) for k in ('board', 'port', 'pon_type', 'admin_status', 'status', 'description', 'tx_power', 'onus',
                                                          'online', 'average_signal', 'last_seen')},
                              'down': down(p.admin_status, p.status), **signals.get((p.board, p.port), {'critical': 0, 'warning': 0}),
                              'outage': next((o.cause for o in active if (o.board, o.port) == (p.board, p.port)), None)} for p in pons]
        view['uplink_ports'] = [{**{k: getattr(u, k) for k in ('name', 'description', 'kind', 'admin_status', 'status', 'negotiation', 'temperature')},
                                 'down': down(u.admin_status, u.status)} for u in uplinks]
        view['active_outages'] = [outage_view(o) for o in active]
        view['ended_outages'] = [outage_view(o) for o in ended]
        return view

    @api.put('/olts/{olt_id}')
    def assign(olt_id: str, body: OltBody, user=Depends(admin), db: Session = Depends(get_db)):
        row = db.scalar(select(Olt).where(Olt.id == olt_id).with_for_update())
        if not row:raise HTTPException(404, 'OLT no encontrada')
        if body.pop_id:
            pop = db.get(Pop, body.pop_id)
            if not pop or pop.archived:raise HTTPException(422, 'POP no disponible')
            row.pop_id, row.pop_manual = pop.id, True
        else:
            device = db.get(Device, row.device_id) if row.device_id else None
            row.pop_id, row.pop_manual = (device.pop_id if device else None), False
        audit(db, 'olt_pop_assigned', user.id, olt_id);db.commit()
        return {'ok': True}

    @api.put('/smartolt/settings')
    def settings(body: SettingsBody, user=Depends(admin), db: Session = Depends(get_db)):
        integration = db.scalar(select(Integration).where(Integration.kind == 'smartolt').with_for_update())
        if not integration:raise HTTPException(404, 'SmartOLT no está configurado')
        if integration.version != body.version:raise HTTPException(409, 'Actualiza la configuración')
        if body.enabled and not integration.encrypted_secret:raise HTTPException(422, 'Falta la API key')
        if body.timezone:
            try:ZoneInfo(body.timezone)
            except (ZoneInfoNotFoundError, ValueError):raise HTTPException(422, 'Zona horaria no válida')
        integration.events_enabled, integration.events_timezone = body.enabled, body.timezone or None
        integration.version += 1
        audit(db, 'smartolt_enabled' if body.enabled else 'smartolt_disabled', user.id, 'smartolt');db.commit()
        return {'ok': True}

    @api.post('/smartolt/refresh', status_code=202)
    def refresh(user=Depends(admin), db: Session = Depends(get_db)):
        # Each refresh spends calls of the account's hourly budget.
        rate_limit(db, 'smartolt-refresh:' + user.id, 4, 3600)
        row = state(db);stamp = now()
        row.due = {task: stamp for task in INTERVALS if task != 'onus'} | {'onus': (row.due or {}).get('onus', stamp)}
        audit(db, 'smartolt_refresh', user.id, 'smartolt');db.commit()
        return {'ok': True}

    return api
