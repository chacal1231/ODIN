"""Bounded read-only calls; no acknowledgments, source mutations or raw payload storage."""
import json
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlencode
from ..inventory.connectors import connector, ConnectorError, clean
from .contracts import EventRecord

PAGE_SIZE=100


@dataclass
class Batch:
    events: list
    progress: dict | None
    coverage: str = 'partial'
    reason: str | None = 'pagination'


def number(value):
    if isinstance(value,bool) or not str(value).isdigit():raise ConnectorError('invalid_response')
    return int(value)


def rows(value):
    if not isinstance(value,list) or any(not isinstance(x,dict) for x in value):raise ConnectorError('invalid_response')
    return value


def local_time(value,zone):
    try:
        stamp=datetime.fromisoformat(str(value))
        if stamp.tzinfo is None:stamp=stamp.replace(tzinfo=ZoneInfo(zone))
        return int(stamp.timestamp())
    except (ValueError,TypeError,KeyError):raise ConnectorError('invalid_source_time')


def libre_status(state):
    state=number(state)
    if state==0:return 'recovered'
    if state in (1,2,3,4,5):return 'open'
    raise ConnectorError('unknown_source_state')


class ZabbixEvents:
    def __init__(self,endpoint,secret,networks,renew):
        self.api=connector('zabbix',endpoint,secret,networks,renew)

    def details(self,records,observed=None,withdrawn=(),observed_at=0):
        recovery_ids={str(r['r_eventid']) for r in records if number(r.get('r_eventid',0))}
        recoveries={str(r['eventid']):number(r['clock']) for r in rows(self.api.rpc('event.get',{
            'output':['eventid','clock'],'eventids':sorted(recovery_ids)}))} if recovery_ids else {}
        if set(recoveries)!=recovery_ids:raise ConnectorError('recovery_unavailable')
        result=[]
        for row in records:
            key=str(number(row['eventid']));clock=number(row['clock']);severity=number(row['severity'])
            recovery=str(row.get('r_eventid','0'));hosts=rows(row.get('hosts',[]))
            # A multi-host expression is not arbitrarily assigned to its first host.
            entity=str(hosts[0]['hostid']) if len(hosts)==1 else None
            # Hosts resolve through the trigger: deleting it leaves open events without hosts.
            # That is not a source revision, so keep the association already observed.
            if not hosts and observed:entity=observed.get(key)
            updated=max([clock,recoveries.get(recovery,0)]+[number(a['clock']) for a in rows(row.get('acknowledges',[]))])
            # Zabbix records no deletion time; the withdrawal is dated when ODIN observed it.
            if key in withdrawn:updated=max(updated,observed_at)
            status='recovered' if recovery in recoveries else 'withdrawn' if key in withdrawn else 'open'
            result.append(EventRecord(external_id=key,entity_id=entity,occurred_at=clock,source_updated_at=updated,
                kind='zabbix_problem',status=status,
                severity='critical' if severity>=4 else 'warning' if severity>=2 else 'info',
                original_severity=str(severity),original_state='0' if recovery in recoveries else '1',
                title=clean(row.get('name'),500) or 'Problema Zabbix',reference='eventid:'+key,
                condition_key='trigger:'+str(number(row['objectid']))))
        return result

    def withdrawn(self,records):
        """Unrecovered events whose trigger no longer exists and that Zabbix no longer lists as problems."""
        candidates={str(number(r['eventid'])):str(number(r['objectid'])) for r in records
                    if not rows(r.get('hosts',[])) and not number(r.get('r_eventid',0))}
        if not candidates:return set()
        triggers={str(t['triggerid']) for t in rows(self.api.rpc('trigger.get',{'output':['triggerid'],
            'triggerids':sorted(set(candidates.values()))}))}
        problems={str(p['eventid']) for p in rows(self.api.rpc('problem.get',{'output':['eventid'],
            'eventids':sorted(candidates)}))}
        return {key for key,trigger in candidates.items() if trigger not in triggers and key not in problems}

    def get(self,params):
        return rows(self.api.rpc('event.get',{'output':['eventid','clock','objectid','severity','r_eventid','name'],
            'source':0,'object':0,'value':1,'selectHosts':['hostid'],
            'selectAcknowledges':['clock','action'],**params}))

    def read(self,progress,known):
        phase=progress['phase'];last=progress.get('last',0)
        if phase=='history':
            params={'eventid_from':str(last+1),'sortfield':'eventid','sortorder':'ASC','limit':PAGE_SIZE}
            if not progress.get('incremental'):
                params.update(time_from=progress['start'],time_till=progress['end'])
            data=self.get(params)
        elif phase=='problems':
            problems=rows(self.api.rpc('problem.get',{'output':['eventid'],'source':0,'object':0,
                'eventid_from':str(last+1),'sortfield':'eventid','sortorder':'ASC','limit':PAGE_SIZE}))
            ids=[str(number(x['eventid'])) for x in problems]
            data=self.get({'eventids':ids}) if ids else []
            if {str(x['eventid']) for x in data}!=set(ids):raise ConnectorError('incomplete_snapshot')
        else:
            ids=[e.external_id for e in known]
            data=self.get({'eventids':ids}) if ids else []
            if {str(x['eventid']) for x in data}!=set(ids):raise ConnectorError('event_not_visible')
            events=self.details(data,{e.external_id:e.entity_id for e in known},self.withdrawn(data),progress['end'])
            if len(known)==PAGE_SIZE:return Batch(events,{**progress,'last':known[-1].id})
            return Batch(events,{'caught_up':True,'last_event_id':progress.get('high_water',0),
                'start':progress['start'],'end':progress['end']},'complete',None)
        ids=[number(r['eventid']) for r in data]
        if len(ids)>PAGE_SIZE or len(set(ids))!=len(ids) or any(x<=last for x in ids):raise ConnectorError('invalid_pagination')
        events=self.details(data)
        if phase=='history':progress={**progress,'high_water':max(ids,default=last)}
        if len(data)==PAGE_SIZE:return Batch(events,{**progress,'last':max(ids)})
        return Batch(events,{**progress,'phase':'problems' if phase=='history' else 'reconcile','last':0 if phase=='history' else ''})


class LibreEvents:
    def __init__(self,endpoint,secret,networks,renew,zone):
        self.api=connector('librenms',endpoint,secret,networks,renew);self.zone=zone

    def request(self,path,key):
        data=self.api.http.request('GET',self.api.base+path,headers=self.api.headers)
        if data.get('status')!='ok':raise ConnectorError('invalid_response')
        result=rows(data.get(key))
        if number(data.get('count',len(result)))!=len(result):raise ConnectorError('incomplete_snapshot')
        if data.get('next') or data.get('next_page_url') or (data.get('links') or {}).get('next'):
            raise ConnectorError('unsupported_pagination')
        return result,data

    def snapshots(self,observed,existing):
        data,_=self.request('/alerts?'+urlencode({'state':'0,1,2,3,4,5','order':'id asc'}),'alerts')
        # This endpoint has no server-side pagination. Fail visibly above the bounded snapshot budget.
        if len(data)>500:raise ConnectorError('snapshot_limit')
        events=[]
        for row in data:
            key=str(number(row['id']));stamp=local_time(row['timestamp'],self.zone)
            previous=existing.get(key);occurred=previous.occurred_at if previous else stamp
            if stamp<occurred:raise ConnectorError('source_time_regressed')
            severity=str(row.get('severity','unknown'))
            events.append(EventRecord(external_id=key,entity_id=str(number(row['device_id'])),
                occurred_at=occurred,source_updated_at=stamp,kind='librenms_alert',time_basis='snapshot',
                observation_sequence=observed,status=libre_status(row['state']),original_state=str(row['state']),
                severity=severity if severity in ('warning','critical') else 'info' if severity=='ok' else 'unknown',
                original_severity=severity,title=clean(row.get('name'),500) or 'Alerta LibreNMS',reference='alert:'+key,
                condition_key='device:'+str(number(row['device_id']))+':rule:'+str(number(row['rule_id']))))
        return Batch(events,None,'partial','snapshot_only')

    def history(self,progress):
        low=progress.get('last',0)+1;high=low+PAGE_SIZE-1
        # Numeric ID windows avoid offset shifts and ties in timestamp-only source ordering.
        data,meta=self.request('/logs/alertlog?'+urlencode({'from':low,'to':high,'limit':PAGE_SIZE,'sortorder':'ASC'}),'logs')
        if number(meta.get('total',len(data)))!=len(data) or len(data)>PAGE_SIZE:raise ConnectorError('incomplete_history_page')
        ids=[number(x['id']) for x in data]
        if len(set(ids))!=len(ids) or any(x<low or x>high for x in ids):raise ConnectorError('invalid_pagination')
        events=[]
        for row in sorted(data,key=lambda x:number(x['id'])):
            stamp=local_time(row['time_logged'],self.zone);key=str(number(row['id'])).zfill(20)
            rule=str(number(row['rule_id']));device=str(number(row['device_id']))
            # Historical severity is unknown unless provided by that historical record itself.
            severity=str(row.get('severity','unknown'))
            events.append(EventRecord(external_id=key,entity_id=device,occurred_at=stamp,source_updated_at=stamp,
                kind='librenms_history',status=libre_status(row['state']),original_state=str(row['state']),
                severity=severity if severity in ('warning','critical') else 'unknown',original_severity=severity,
                title='Regla '+rule+' · '+('recuperación' if number(row['state'])==0 else 'alerta'),
                reference='alertlog:'+str(number(row['id'])),condition_key='device:'+device+':rule:'+rule))
        if progress['start']==0 and events:progress={**progress,'start':min(e.occurred_at for e in events)}
        # A one-row existence probe covers sparse/deleted ID ranges without advancing over unknown data.
        tail,_=self.request('/logs/alertlog?'+urlencode({'from':high+1,'limit':1}),'logs')
        if any(number(r['id'])<=high for r in tail):raise ConnectorError('invalid_pagination')
        return Batch(events,{'last':high,'start':progress['start'],'end':progress['end']} if tail else
            {'last':max(ids,default=progress.get('last',0)),'start':progress['start'],'end':progress['end'],'caught_up':True},
            'partial','pagination' if tail else 'retained_history')
