"""Setup assistant: each step of putting ODIN to work, what it found, and the checks it suggests.

Steps follow the order in which each source gives the next one its meaning: LibreNMS brings devices and port
descriptions; the descriptions name the providers (see inventory/roles.py); Zabbix and Graylog bring events and
logs; SmartOLT brings the OLTs; then the checks, Telegram and the model. The state of each step is read from
what ODIN holds; the assistant stores nothing of its own.
Suggested checks are the management address of each own device and the far end of each carrier link (the other
address of its /31, /127 or IPv4 /30). Applying them goes through the same validation as any check.
"""
import ipaddress
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from .db import get_db
from .models import (Device, Integration, InventoryInterface, Olt, ProbeTarget, SourceIdentity, TelegramState)
from .inventory import roles
from .inventory.service import integration_view
from .security import audit

CARRIERS = ('transit', 'transport', 'ixp', 'peering')
# Templates seeded in probes/config.py.
PE_PROFILE = '00000000-0000-4000-8000-000000000012'
LINK_PROFILE = '00000000-0000-4000-8000-000000000014'
SOURCES = {'librenms': 'LibreNMS', 'zabbix': 'Zabbix', 'graylog': 'Graylog', 'smartolt': 'SmartOLT'}


class Suggestion(BaseModel):
    address: str = Field(max_length=45)
    name: str = Field(min_length=1, max_length=120)


class ApplyBody(BaseModel):
    items: list[Suggestion] = Field(min_length=1, max_length=100)


def source_step(db, kind, title, purpose, optional=False):
    row = db.get(Integration, kind)
    view = integration_view(row)
    state = {'not_configured': 'pending', 'configured_not_validated': 'pending', 'error': 'error'}.get(view['status'], 'done')
    if kind in ('librenms', 'zabbix') and state == 'done' and not (row and row.synced_at):state = 'pending'
    if kind in ('graylog', 'smartolt') and state == 'done' and not (row and row.events_enabled):state = 'pending'
    return {'id': kind, 'title': title, 'purpose': purpose, 'optional': optional, 'state': state, 'status': view['status'],
            'last_error': view['last_error'], 'configured': bool(row and row.encrypted_secret), 'reading': bool(row and row.events_enabled),
            'checked_at': view['checked_at'], 'synced_at': view['synced_at']}


def far_end(network):
    """The other address of a point-to-point subnet (/31, /127, or a /30 in IPv4); None when it cannot be told apart."""
    net = network.network
    if net.prefixlen == net.max_prefixlen - 1:
        others = [ip for ip in (net.network_address, net.broadcast_address) if ip != network.ip]
    elif net.version == 4 and net.prefixlen == 30:
        others = [ip for ip in net.hosts() if ip != network.ip]
    else:
        # A /126 has three usable addresses and a /29 many neighbors: guessing would probe the wrong one.
        return None
    return str(others[0]) if len(others) == 1 else None


def providers(db):
    network = roles.Network(db)
    groups, unclassified, customers, internal = {}, [], 0, 0
    for port in network.ports:
        role, provider, details = roles.classify(port.description)
        other = far_end(port.network)
        far = network.owner.get(ipaddress.ip_address(other)) if other else None
        entry = {'device_id': port.device_id, 'device_name': port.device_name, 'interface': port.name, 'description': port.description,
                 'network': str(port.network)}
        if far and far.device_id != port.device_id:
            internal += 1;continue
        if role == 'customer':
            customers += 1;continue
        if role in CARRIERS or role == 'cdn':
            # Transports are named by their whole description: the provider is not always its second word.
            name = port.description if role == 'transport' else (provider or port.description)
            groups.setdefault((role, name), []).append(entry)
        elif not role:
            unclassified.append(entry)
    return {'groups': [{'role': role, 'label': roles.LABELS.get(role, role), 'provider': name, 'ports': ports}
                       for (role, name), ports in sorted(groups.items())],
            'unclassified': sorted(unclassified, key=lambda x: (x['device_name'], x['interface']))[:200], 'unclassified_count': len(unclassified),
            'customers': customers, 'internal': internal, 'ports': len(network.ports)}


def suggestions(db):
    from .probes.config import safe_ip
    monitored = {ip for row in db.scalars(select(ProbeTarget.approved_ips).where(ProbeTarget.archived.is_(False))) for ip in row or []}
    names = {n.lower() for n in db.scalars(select(ProbeTarget.name).where(ProbeTarget.archived.is_(False)))}
    result, seen = [], set()

    def add(kind, address, name, device, extra):
        try:address = safe_ip(address)
        except ValueError:return
        if address in monitored or address in seen or name.lower() in names:return
        seen.add(address);names.add(name.lower())
        result.append({'kind': kind, 'address': address, 'name': name[:120], 'device_id': device.id if device else None,
                       'device_name': device.name if device else '', 'pop_id': device.pop_id if device else None, **extra})

    devices = {d.id: d for d in db.scalars(select(Device).where(Device.archived.is_(False), Device.merged_into.is_(None)))}
    for identity in db.scalars(select(SourceIdentity).where(SourceIdentity.present.is_(True)).order_by(SourceIdentity.source, SourceIdentity.name, SourceIdentity.id)):
        device = devices.get(identity.device_id)
        for value in identity.addresses or []:
            try:address = str(ipaddress.ip_interface(value).ip)
            except ValueError:continue
            if device:add('device', address, device.name, device, {'source': identity.source})
            break
    network = roles.Network(db)
    for port in network.ports:
        role, provider, _ = roles.classify(port.description)
        if role not in CARRIERS:continue
        address = far_end(port.network)
        if not address or network.owner.get(ipaddress.ip_address(address)):continue
        who = port.description if role == 'transport' else provider or port.description
        add('carrier', address, f'{roles.LABELS[role].capitalize()} {who} · {port.device_name} {port.name}', devices.get(port.device_id),
            {'role': role, 'provider': who, 'interface': port.name})
    return result


def steps(db):
    count = lambda q: db.scalar(select(func.count()).select_from(q.subquery()))
    found = providers(db)
    targets = count(select(ProbeTarget.id).where(ProbeTarget.archived.is_(False), ProbeTarget.enabled.is_(True)))
    telegram, tg = db.get(Integration, 'telegram'), db.get(TelegramState, 'telegram')
    from .analyst.service import active
    model = active(db)
    result = [
        {**source_step(db, 'librenms', 'LibreNMS: equipos y puertos',
                       'Trae los equipos, sus puertos con IP y la descripción de cada puerto, de donde ODIN saca los proveedores.'),
         'devices': count(select(SourceIdentity.id).where(SourceIdentity.source == 'librenms', SourceIdentity.present.is_(True))),
         'ports': count(select(InventoryInterface.id).join(SourceIdentity, SourceIdentity.id == InventoryInterface.identity_id)
                        .where(SourceIdentity.source == 'librenms', InventoryInterface.kind == 'port', InventoryInterface.present.is_(True)))},
        {'id': 'providers', 'title': 'Proveedores detectados', 'optional': False,
         'purpose': 'ODIN reconoce tránsitos, transportes, IXP y peering por la descripción del puerto. Revisa que estén todos.',
         'state': 'done' if found['groups'] else 'pending', 'groups': len(found['groups']), 'unclassified': found['unclassified_count']},
        {**source_step(db, 'zabbix', 'Zabbix: hosts y eventos', 'Suma hosts y sus alertas: problemas que ODIN asocia a los casos.', True),
         'devices': count(select(SourceIdentity.id).where(SourceIdentity.source == 'zabbix', SourceIdentity.present.is_(True)))},
        source_step(db, 'graylog', 'Graylog: logs de los routers',
                    'Los cambios de estado de BGP e interfaces que generan los casos. Actívalo en Eventos.'),
        {**source_step(db, 'smartolt', 'SmartOLT: OLTs y PON', 'Caídas de PON, uplinks y señal de las ONUs. Actívalo en Monitoreo → OLTs.', True),
         'olts': count(select(Olt.id).where(Olt.present.is_(True)))},
        {'id': 'checks', 'title': 'Destinos de monitoreo', 'optional': False,
         'purpose': 'Sondeos propios hacia tus equipos y hacia el otro extremo de cada enlace con proveedores.',
         'state': 'done' if targets else 'pending', 'targets': targets, 'suggested': len(suggestions(db))},
        {'id': 'telegram', 'title': 'Telegram: grupos del NOC y de proveedores', 'optional': True,
         'purpose': 'Avisos de los casos con su diagnóstico, y un hilo por cada falla con un proveedor.',
         'state': 'done' if telegram and telegram.encrypted_secret and tg and tg.chat_id else 'pending',
         'configured': bool(telegram and telegram.encrypted_secret), 'group': bool(tg and tg.chat_id), 'providers_group': bool(tg and tg.providers_chat_id)},
        {'id': 'model', 'title': 'Modelo del analista', 'optional': True,
         'purpose': 'Explica cada caso citando la evidencia. Local (llama.cpp) o DeepSeek por API.',
         'state': 'done' if model else 'pending', 'provider': model.kind if model else None},
    ]
    return result


def router(current, admin):
    api = APIRouter(prefix='/api/v1/setup')

    @api.get('')
    def status(user=Depends(admin), db: Session = Depends(get_db)):
        result = steps(db)
        required = [s for s in result if not s['optional']]
        return {'steps': result, 'done': sum(s['state'] == 'done' for s in result), 'total': len(result),
                'required_done': all(s['state'] == 'done' for s in required)}

    @api.get('/providers')
    def detected(user=Depends(admin), db: Session = Depends(get_db)):
        return providers(db)

    @api.get('/suggestions')
    def suggested(user=Depends(admin), db: Session = Depends(get_db)):
        return suggestions(db)

    @api.post('/suggestions')
    def apply(body: ApplyBody, user=Depends(admin), db: Session = Depends(get_db)):
        from .probes.api import TargetBody, save_target
        offered = {s['address']: s for s in suggestions(db)}
        created, failed = [], []
        for chosen in body.items:
            found = offered.get(chosen.address)
            if not found:
                failed.append({'address': chosen.address, 'error': 'Ya no es una sugerencia vigente'});continue
            try:
                save_target(db, TargetBody(name=chosen.name, address=found['address'], categories=['red_propia'], pop_id=found['pop_id'],
                                           profile_version_id=PE_PROFILE if found['kind'] == 'device' else LINK_PROFILE, enabled=True,
                                           description='Sugerido por el asistente de puesta en marcha'), user)
                created.append(found['address'])
            except HTTPException as exc:
                db.rollback();failed.append({'address': chosen.address, 'error': exc.detail})
        audit(db, 'setup_checks_created', user.id, str(len(created)));db.commit()
        return {'created': created, 'failed': failed}

    return api
