import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from sqlalchemy import select,func
from odin.db import session
from odin.models import Integration,EventCollector,EventStream,SourceEvent,Job,now
from odin.security import encrypt
from odin.jobs import claim
from odin.events.connectors import ZabbixEvents,LibreEvents,Batch,local_time,libre_status
from odin.events.collector import schedule,execute,progress_for,link_recoveries
from odin.events.contracts import EventRecord
from odin.inventory.connectors import ConnectorError
from test_access import enroll


def config(source='zabbix',**kwargs):
    with session() as db:
        row=Integration(kind=source,endpoint='https://example.test',encrypted_secret=encrypt('fake-test-token'),
            allowed_networks=['192.0.2.0/24'],events_enabled=True,events_timezone='America/Bogota' if source=='librenms' else None,**kwargs)
        db.add(row);db.commit()


def problem(key='10',recovery='0',clock='100'):
    return {'eventid':key,'objectid':'5','clock':clock,'severity':'4','r_eventid':recovery,'name':'Problema',
        'hosts':[{'hostid':'1'}],'acknowledges':[]}


def zabbix(responses):
    adapter=object.__new__(ZabbixEvents);adapter.api=Mock();adapter.api.rpc=Mock(side_effect=responses);return adapter


def libre(responses):
    adapter=object.__new__(LibreEvents);adapter.zone='America/Bogota';adapter.api=Mock();adapter.api.base='https://example.test/api/v0'
    adapter.api.headers={};adapter.api.http.request=Mock(side_effect=responses);return adapter


def alert(state=1,stamp='2026-09-27 09:00:00'):
    return {'id':2,'device_id':3,'rule_id':4,'state':state,'timestamp':stamp,'severity':'critical','name':'Alerta'}


def log(key=1,state=1):
    return {'id':key,'device_id':3,'rule_id':4,'state':state,'time_logged':'2026-09-27 09:00:00'}


def payload(key,values,**extra):return {'status':'ok','count':len(values),key:values,**extra}


def test_zabbix_recovery_uses_explicit_reference_and_clock():
    a=zabbix([[problem(recovery='20')],[{'eventid':'20','clock':'200'}]])
    result=a.read({'phase':'history','last':0,'start':0,'end':300},[])
    assert result.events[0].status=='recovered' and result.events[0].source_updated_at==200
    assert result.events[0].severity=='critical' and result.events[0].original_severity=='4'
    assert result.events[0].entity_id=='1'
    assert result.progress['phase']=='problems'
    assert all(call.args[0] in ('event.get','problem.get') for call in a.api.rpc.call_args_list)


def test_zabbix_missing_recovery_does_not_invent_time():
    a=zabbix([[problem(recovery='20')],[]])
    with pytest.raises(ConnectorError,match='recovery_unavailable'):a.read({'phase':'history','last':0,'start':0,'end':300},[])


def test_reconciliation_checks_older_open_events_even_outside_window():
    a=zabbix([[problem(clock='1',recovery='20')],[{'eventid':'20','clock':'300'}]])
    b=a.read({'phase':'reconcile','last':'','start':200,'end':400},[SimpleNamespace(external_id='10',id='a',entity_id='1')])
    assert b.coverage=='complete' and b.progress['caught_up'] and b.events[0].status=='recovered'
    assert a.api.rpc.call_args_list[0].args[1]['eventids']==['10']


def test_deleted_trigger_withdraws_event_keeping_observed_host():
    # Zabbix keeps unrecovered events of a deleted trigger but no longer resolves their host.
    a=zabbix([[{**problem(),'hosts':[]}],[],[]])
    b=a.read({'phase':'reconcile','last':'','start':0,'end':400},[SimpleNamespace(external_id='10',id='a',entity_id='1')])
    e=b.events[0]
    assert e.status=='withdrawn' and e.entity_id=='1' and e.source_updated_at==400 and e.occurred_at==100
    assert [c.args[0] for c in a.api.rpc.call_args_list]==['event.get','trigger.get','problem.get']


@pytest.mark.parametrize('triggers,problems',[([{'triggerid':'5'}],[]),([],[{'eventid':'10'}])])
def test_event_without_host_stays_open_unless_source_confirms_withdrawal(triggers,problems):
    a=zabbix([[{**problem(),'hosts':[]}],triggers,problems])
    b=a.read({'phase':'reconcile','last':'','start':0,'end':400},[SimpleNamespace(external_id='10',id='a',entity_id='1')])
    assert b.events[0].status=='open' and b.events[0].entity_id=='1' and b.events[0].source_updated_at==100


def test_absent_zabbix_event_is_not_a_recovery():
    a=zabbix([[]])
    with pytest.raises(ConnectorError,match='event_not_visible'):
        a.read({'phase':'reconcile','last':'','start':0,'end':400},[SimpleNamespace(external_id='10')])


def test_libre_snapshot_includes_recovered_and_acknowledged():
    a=libre([payload('alerts',[alert(0),{**alert(2),'id':3}])])
    b=a.snapshots(1000,{})
    assert [e.status for e in b.events]==['recovered','open']
    assert all(e.time_basis=='snapshot' for e in b.events)
    assert b.coverage=='partial' and b.reason=='snapshot_only'
    assert '0%2C1%2C2%2C3%2C4%2C5' in a.api.http.request.call_args.args[1]
    assert local_time('2026-09-27 09:00:00','America/Bogota')==local_time('2026-09-27T14:00:00+00:00','UTC')


def test_unknown_state_never_means_recovered():
    with pytest.raises(ConnectorError):libre_status(99)
    for state in [1,2,3,4,5]:assert libre_status(state)=='open'


def test_history_windows_by_id_ignore_timestamp_order_and_ties():
    a=libre([payload('logs',[log(2,0),log(1)],total=2),payload('logs',[log(101)],total=1)])
    b=a.history({'last':0,'start':0,'end':200})
    assert [e.external_id for e in b.events]==[str(1).zfill(20),str(2).zfill(20)]
    assert b.progress['last']==100 and b.reason=='pagination'
    assert all(e.severity=='unknown' for e in b.events)
    assert 'from=1&to=100' in a.api.http.request.call_args_list[0].args[1]


def test_truncated_history_and_out_of_range_ids_rejected():
    for records,total in [([log(1)],2),([log(101)],1)]:
        a=libre([payload('logs',records,total=total)])
        with pytest.raises(ConnectorError):a.history({'last':0,'start':0,'end':200})


def test_history_recovery_links_to_same_condition_on_first_page():
    a=libre([payload('logs',[log(1),log(2,0)],total=2),payload('logs',[],total=0)])
    b=a.history({'last':0,'start':0,'end':200})
    with session() as db:link_recoveries(db,None,b.events)
    assert b.events[1].recovery_of==b.events[0].external_id


def next_job():
    with session() as db:schedule(db);return claim(db)


def test_schedule_is_idempotent_and_paused_sources_not_queued():
    config()
    with session() as db:
        schedule(db);schedule(db)
        assert db.scalar(select(func.count()).select_from(Job))==1
        db.get(Integration,'zabbix').events_enabled=False;db.commit()
    job=next_job();execute(job.id,job.lease_token)
    with session() as db:
        assert db.get(Job,job.id).result['status']=='cancelled'
        assert db.scalar(select(func.count()).select_from(EventStream))==0


def test_poll_persists_failure_without_advancing_and_retries(monkeypatch):
    config();job=next_job()
    monkeypatch.setattr('odin.events.collector.ZabbixEvents',Mock(side_effect=ConnectorError('connection_failed')))
    execute(job.id,job.lease_token)
    with session() as db:
        stream=db.scalar(select(EventStream));collector=db.scalar(select(EventCollector));j=db.get(Job,job.id)
        assert stream.coverage=='unavailable' and stream.cursor is None and stream.complete_through is None
        assert collector.last_error=='connection_failed' and j.status=='pending' and j.run_after>now()


def test_success_checkpoint_and_job_finish_are_atomic(monkeypatch):
    config();job=next_job()
    event=EventRecord(external_id='1',occurred_at=100,source_updated_at=100,kind='zabbix_problem',status='open',
        severity='warning',original_severity='2',title='Prueba',reference='eventid:1')
    adapter=Mock();adapter.read.return_value=Batch([event],None,'complete',None)
    monkeypatch.setattr('odin.events.collector.ZabbixEvents',lambda *_:adapter)
    execute(job.id,job.lease_token);execute(job.id,job.lease_token)
    with session() as db:
        assert db.scalar(select(func.count()).select_from(SourceEvent))==1
        assert db.get(Job,job.id).status=='done'
        assert db.scalar(select(EventStream)).coverage=='complete'
        assert db.scalar(select(EventCollector)).last_error is None


def test_changed_config_fences_inflight_result(monkeypatch):
    config();job=next_job()
    def read(*args):
        with session() as db:i=db.get(Integration,'zabbix');i.version+=1;db.commit()
        return Batch([],None,'complete',None)
    adapter=Mock();adapter.read.side_effect=read
    monkeypatch.setattr('odin.events.collector.ZabbixEvents',lambda *_:adapter)
    execute(job.id,job.lease_token)
    with session() as db:
        assert db.scalar(select(func.count()).select_from(EventStream))==0
        assert db.get(Job,job.id).result['status']=='cancelled'


def test_settings_access_and_timezone(client,administrator):
    config('librenms');enroll(client)
    assert client.put('/api/v1/event-sources/librenms',json={'version':1,'enabled':True}).status_code==422
    assert client.put('/api/v1/event-sources/librenms',json={'version':1,'enabled':True,'timezone':'America/Bogota'}).status_code==200
    assert client.put('/api/v1/event-sources/librenms',json={'version':1,'enabled':False}).status_code==409
    body=client.get('/api/v1/event-sources').json()
    assert next(r for r in body if r['source']=='librenms')['enabled'] is True
    from odin.models import User
    with session() as db:db.get(User,administrator).role='operator';db.commit()
    assert client.put('/api/v1/event-sources/librenms',json={'version':2,'enabled':False}).status_code==403
    assert client.post('/api/v1/event-sources/librenms/retry').status_code==403
    assert client.get('/api/v1/event-sources').status_code==200


def test_snapshot_recovery_with_unchanged_source_timestamp_and_old_reply():
    from odin.events.service import ingest_page
    from odin.events.contracts import EventPageInput
    first=libre([payload('alerts',[alert(1)])]).snapshots(1000,{}).events[0]
    recovery=libre([payload('alerts',[alert(0)])]).snapshots(2000,{'2':first}).events[0]
    assert first.source_updated_at==recovery.source_updated_at
    for version,record in enumerate([first,recovery,first]):
        with session() as db:
            ingest_page(db,'librenms','alerts',EventPageInput(request_key=str(version),expected_version=version,
                window_start=0,window_end=now(),events=[record],coverage='partial',reason='snapshot_only'))
            db.commit()
    with session() as db:
        stored=db.scalar(select(SourceEvent))
        assert stored.status=='recovered' and stored.observation_sequence==2000
        assert db.scalar(select(func.count()).select_from(SourceEvent))==1


def test_endpoint_change_uses_new_stream_identity():
    config()
    with session() as db:
        schedule(db);before=db.scalar(select(EventCollector)).stream_name
        i=db.get(Integration,'zabbix');i.endpoint='https://new.example.test';i.version+=1;db.commit()
        schedule(db)
        assert db.scalar(select(EventCollector)).stream_name!=before


def test_failure_does_not_prevent_other_source_scheduling(monkeypatch):
    config();config('librenms')
    with session() as db:
        schedule(db)
        jobs=list(db.scalars(select(Job)))
        assert {j.payload['collector_id'] for j in jobs}=={'zabbix:events','librenms:alerts','librenms:alert_history'}


def test_expired_lease_cannot_advance_checkpoint(monkeypatch):
    config();job=next_job()
    def read(*args):
        with session() as db:j=db.get(Job,job.id);j.lease_until=now()-1;db.commit()
        return Batch([],None,'complete',None)
    adapter=Mock();adapter.read.side_effect=read
    monkeypatch.setattr('odin.events.collector.ZabbixEvents',lambda *_:adapter)
    execute(job.id,job.lease_token)
    with session() as db:assert db.scalar(select(func.count()).select_from(EventStream))==0


def test_zabbix_new_ids_with_old_timestamps_are_not_lost():
    a=zabbix([[problem(key='50',clock='1')]])
    b=a.read({'phase':'history','last':49,'high_water':49,'incremental':True,'start':200,'end':400},[])
    assert b.events[0].occurred_at==1 and b.progress['high_water']==50
    assert 'time_from' not in a.api.rpc.call_args_list[0].args[1]
    stream=SimpleNamespace(cursor=json.dumps({'caught_up':True,'last_event_id':50,'start':200,'end':400}))
    resumed=progress_for(stream,'zabbix','events',500)
    assert resumed['last']==50 and resumed['incremental']


def test_missing_snapshot_alert_is_marked_absent_not_recovered(client,administrator,monkeypatch):
    from odin.events.service import ingest_page
    from odin.events.contracts import EventPageInput
    record=libre([payload('alerts',[alert(1)])]).snapshots(1000,{}).events[0]
    for version,events,stamp in [(0,[record],1000),(1,[],2000)]:
        monkeypatch.setattr('odin.events.service.now',lambda:stamp)
        with session() as db:
            ingest_page(db,'librenms','alerts',EventPageInput(request_key=str(version),expected_version=version,
                window_start=0,window_end=now(),events=events,coverage='partial',reason='snapshot_only'))
            db.commit()
    enroll(client)
    row=client.get('/api/v1/source-events').json()['items'][0]
    assert row['status']=='open' and row['present_in_latest_snapshot'] is False
