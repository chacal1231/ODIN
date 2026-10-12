"""Read models for NOC navigation; grouping never asserts a shared root cause."""
from collections import Counter
from sqlalchemy import select
from ..models import (MonitoringDestination, ProbeTarget, Device, Pop, InventoryLink,
                      InventoryInterface, ProbeIncident, ProbeIncidentEvent, now)
from .service import target_views, origin_view


def ensure_destination(db,target):
    if target.destination_id:return db.get(MonitoringDestination,target.destination_id)
    scope='network' if 'red_propia' in (target.categories or []) else 'external'
    matches=list(db.scalars(select(Device).where(Device.archived.is_(False),Device.name==target.name))) if scope=='network' else []
    row=MonitoringDestination(id=target.id,name=target.name,scope=scope,device_id=matches[0].id if len(matches)==1 else None)
    db.add(row);db.flush();target.destination_id=row.id
    return row


def aggregate(items):
    """No healthy group while any required observation is missing."""
    if not items:return 'unmonitored'
    states=[x['state'] for x in items]
    affected=sum(x in ('critical','warning','partial') for x in states)
    if affected:return 'partial' if affected<len(states) or 'partial' in states else ('critical' if 'critical' in states else 'warning')
    if 'no_data' in states or 'unmonitored' in states:return 'no_data'
    for value in ('pending_validation','paused','maintenance','warming'):
        if value in states:return value
    return 'healthy'


def overview(db):
    stamp=now()
    targets=list(db.scalars(select(ProbeTarget).where(ProbeTarget.archived.is_(False)).order_by(ProbeTarget.name)))
    checks=target_views(db,targets,stamp)
    destinations={d.id:d for d in db.scalars(select(MonitoringDestination))}
    devices={d.id:d for d in db.scalars(select(Device))}
    pops={p.id:p for p in db.scalars(select(Pop).where(Pop.archived.is_(False)))}
    links=list(db.scalars(select(InventoryLink).where(InventoryLink.archived.is_(False))))
    ports={p.id:p for p in db.scalars(select(InventoryInterface).where(InventoryInterface.id.in_({x for l in links for x in (l.left_port_id,l.right_port_id) if x})))}
    grouped={}
    for check in checks:grouped.setdefault(check['destination_id'] or check['id'],[]).append(check)
    result=[]
    for key,group in grouped.items():
        entity=destinations.get(key);first=next((c for c in group if c['catalog_metadata']),group[0])
        device=devices.get(entity.device_id) if entity else None
        # Follow reconciled inventory identities without losing the monitoring link.
        visited=set()
        while device and device.merged_into and device.id not in visited:
            visited.add(device.id);device=devices.get(device.merged_into)
        pop_id=device.pop_id if device and not device.archived else first['pop_id']
        essential=[c for c in group if c['essential']]
        result.append({'id':key,'name':entity.name if entity else first['name'],
            'scope':entity.scope if entity else ('network' if 'red_propia' in first['categories'] else 'external'),
            'device_id':device.id if device and not device.archived else None,
            'device_name':device.name if device else None,'link_id':entity.link_id if entity else None,
            'version':entity.version if entity else 1,'pop_id':pop_id,'pop_name':pops[pop_id].name if pop_id in pops else 'Sin POP',
            'provider':first['catalog_metadata'].get('provider') or first['service'] or 'Sin proveedor',
            'region':first['catalog_metadata'].get('region') or first['catalog_metadata'].get('scope') or 'Sin región',
            'categories':sorted({c for x in group for c in x['categories']}),
            'state':aggregate(essential),'checks':group,'check_count':len(group),
            'optional_issues':sum(c['state']!='healthy' for c in group if not c['essential']),
            'incidents':sum(c['active_incidents'] for c in group)})
    target_context={c['id']:d for d in result for c in d['checks']}
    active=list(db.scalars(select(ProbeIncident).where(ProbeIncident.closed_at.is_(None)).order_by(ProbeIncident.opened_at.desc())))
    # Archiving a check must not hide its unresolved incident from the NOC.
    for target in db.scalars(select(ProbeTarget).where(ProbeTarget.id.in_({i.target_id for i in active}-set(target_context)))):
        entity=destinations.get(target.destination_id)
        target_context[target.id]={'id':target.destination_id or target.id,'name':entity.name if entity else target.name,
            'scope':entity.scope if entity else ('network' if 'red_propia' in target.categories else 'external'),
            'pop_id':target.pop_id,'pop_name':pops[target.pop_id].name if target.pop_id in pops else 'Sin POP',
            'checks':[{'id':target.id,'name':target.name+' (archivada)' if target.archived else target.name}]}
    incident_groups={}
    for incident in active:
        dest=target_context.get(incident.target_id)
        if not dest:continue
        group_key=('pop:'+dest['pop_id']) if dest['scope']=='network' and dest['pop_id'] else ('destination:'+dest['id'])
        group=incident_groups.setdefault(group_key,{'id':group_key,'name':dest['pop_name'] if group_key.startswith('pop:') else dest['name'],
            'scope':dest['scope'],'severity':'warning','items':[]})
        if incident.severity=='critical':group['severity']='critical'
        group['items'].append({'id':incident.id,'target_id':incident.target_id,'destination_id':dest['id'],
            'target_name':next(c['name'] for c in dest['checks'] if c['id']==incident.target_id),
            'address':incident.address,'severity':incident.severity,'conditions':incident.conditions,
            'opened_at':incident.opened_at,'acknowledged':bool(incident.acknowledged_at)})
    groups=sorted(incident_groups.values(),key=lambda g:(g['severity']!='critical',-len(g['items']),g['name']))
    link_views=[]
    for link in links:
        left,right=devices.get(link.left_id),devices.get(link.right_id)
        direct=[d for d in result if d['link_id']==link.id]
        linked_devices=[d for d in result if d['device_id'] in (link.left_id,link.right_id)]
        link_views.append({'id':link.id,'left_id':link.left_id,'right_id':link.right_id,
            'name':f'{left.name if left else "?"} ↔ {right.name if right else "?"}',
            'pop_ids':sorted({d.pop_id for d in (left,right) if d and d.pop_id}),
            'left_port':ports[link.left_port_id].name if link.left_port_id in ports else None,
            'right_port':ports[link.right_port_id].name if link.right_port_id in ports else None,
            'vlans':link.vlans,'state':aggregate(direct),'destinations':[d['id'] for d in direct],
            'endpoint_state':aggregate(linked_devices)})
    network=[d for d in result if d['scope']=='network']
    pop_views=[]
    for key,label in [(p.id,p.name) for p in pops.values()]+[(None,'Sin POP')]:
        members=[d for d in network if d['pop_id']==key]
        inventory=[d for d in devices.values() if not d.archived and d.pop_id==key]
        if not members and not inventory and key is None:continue
        pop_views.append({'id':key,'name':label,'state':aggregate(members),'destinations':len(members),
            'devices':len(inventory),'monitored_devices':len({d['device_id'] for d in members if d['device_id']}),
            'incidents':sum(d['incidents'] for d in members)})
    changes=[]
    events=list(db.scalars(select(ProbeIncidentEvent).order_by(ProbeIncidentEvent.created_at.desc()).limit(12)))
    event_incidents={i.id:i for i in db.scalars(select(ProbeIncident).where(ProbeIncident.id.in_({e.incident_id for e in events})))}
    names={t.id:t.name for t in db.scalars(select(ProbeTarget).where(ProbeTarget.id.in_({i.target_id for i in event_incidents.values()})))}
    for event in events:
        incident=event_incidents[event.incident_id]
        changes.append({'id':event.id,'incident_id':event.incident_id,'kind':event.kind,'at':event.created_at,
            'target_name':names.get(incident.target_id,incident.target_id),'address':incident.address})
    from .verdict import compute as verdict
    origin=origin_view(db,stamp)
    return {'origin':origin,'verdict':verdict(db,checks,stamp,origin['state']=='healthy'),'destinations':result,'pops':pop_views,'links':link_views,
        'devices':[{'id':d.id,'name':d.name,'pop_id':d.pop_id} for d in devices.values() if not d.archived],
        'incident_groups':groups,'recent_changes':changes,
        'health':dict(Counter(c['state'] for c in checks)),
        'counts':{'destinations':len(result),'checks':len(checks),'enabled_checks':sum(c['enabled'] for c in checks),
            'network':len(network),'external':len(result)-len(network),'incidents':len(active)}}
