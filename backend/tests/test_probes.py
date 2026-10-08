from types import SimpleNamespace
from unittest.mock import Mock
import subprocess
import pytest
from sqlalchemy import select,func,text
from odin.db import session
from odin.models import (ProbeTarget,ProbeBatch,ProbeSeries,ProbeRound,ProbeState,ProbeIncident,
    ProbeIncidentEvent,ProbeRollup,ProbeOrigin,now,uid)
from odin.probes.config import ProbeConfig,DEFAULT_VERSION_ID,host,safe_ip
from odin.probes.detector import window_metrics,transition,evaluate
from odin.probes.fping import parse,run
from odin.probes.resolver import ProbeError
from odin.probes.service import claim,slot_at,changed,target_views
from odin.probes.runner import execute
from odin.probes.storage import aggregate_bucket,aggregate_hour,upkeep
from test_access import enroll


def target(db,**kwargs):
    row=ProbeTarget(name='PE-LAB',address='10.20.0.2',family='ipv4',profile_version_id=DEFAULT_VERSION_ID,
        approved_ips=['10.20.0.2'],effective=ProbeConfig().model_dump(),enabled=True,**kwargs)
    db.add(row);db.flush();row.next_due=slot_at(row,now());db.commit();return row


def add_round(db,t,s,slot,received=10,status='valid',maintenance=False,value=12):
    r=ProbeRound(series_id=s.id,slot=slot,batch_id=uid(),status=status,sent=10 if status=='valid' else 0,
        received=received if status=='valid' else 0,samples=[value]*received+[None]*(10-received) if status=='valid' else [],finished_at=slot)
    db.add(r);db.flush();evaluate(db,t,s,r,maintenance);db.commit();return r


@pytest.mark.parametrize('output,code', [('10.20.0.2 : - - -',1),('10.20.0.2 : 0 12.4 -',1),('10.20.0.2 : 1 2 3',0)])
def test_fping_real_format(output,code):
    result=parse(output,code,['10.20.0.2'],3)['10.20.0.2']
    assert result['sent']==3
    assert result['received']==sum(x is not None for x in result['samples'])


@pytest.mark.parametrize('output,code', [('10.20.0.2 : - -',1),('10.20.0.2 : nan 1 2',0),
    ('10.20.0.2 : - - -\nsendto: Network is unreachable',1),('socket: Operation not permitted',4),
    ('10.20.0.2 : - - -',-15),('10.20.0.3 : 1 2 3',0),('10.20.0.2 : -1 1 2',0)])
def test_invalid_execution_is_not_packet_loss(output,code):
    with pytest.raises(ProbeError):parse(output,code,['10.20.0.2'],3)


def test_fping_timeout_is_not_packet_loss(monkeypatch):
    monkeypatch.setattr('odin.probes.fping.subprocess.run',Mock(side_effect=subprocess.TimeoutExpired('fping',8)))
    with pytest.raises(ProbeError,match='probe_timeout'):run(['10.20.0.2'],ProbeConfig().model_dump())


@pytest.mark.parametrize('address',['127.0.0.1','169.254.169.254','::1','fe80::1','224.0.0.1','https://x.test','x.test;id','x.test:80'])
def test_probe_address_validation(address):
    with pytest.raises(ValueError):host(address)


def test_profile_validation_and_p95():
    for values in ({'rtt_warning':20},{'loss_recovery':0},{'loss_recovery':6},{'packets':20,'period_ms':2000},{'interval_seconds':31}):
        with pytest.raises(ValueError):ProbeConfig(**values)
    c=ProbeConfig().model_dump()
    rounds=[SimpleNamespace(status='valid',slot=30,samples=[1]*99,sent=100,received=99),
            SimpleNamespace(status='valid',slot=60,samples=[1000],sent=1,received=1)]
    m=window_metrics(rounds,c,60,30)
    assert m['p95_ms']==1 and m['sent']==101 and m['coverage']==.2
    assert m['loss_percent']==pytest.approx(100/101)


def test_threshold_equality_persistence_and_recovery():
    c=ProbeConfig(rtt_warning=30,rtt_critical=60).model_dump()
    m={'warm':True,'coverage':1,'p95_ms':30,'loss_percent':5}
    conditions,counters=transition({}, {},m,c,10)
    assert not conditions
    conditions,counters=transition(conditions,counters,m,c,10)
    assert conditions=={'loss':'warning','latency':'warning'}
    for _ in range(2):conditions,counters=transition(conditions,counters,{**m,'p95_ms':60,'loss_percent':20},c,8)
    assert conditions=={'loss':'critical','latency':'critical'}
    for _ in range(2):conditions,counters=transition(conditions,counters,{**m,'p95_ms':1,'loss_percent':0},c,10)
    assert conditions
    conditions,counters=transition(conditions,counters,{**m,'p95_ms':1,'loss_percent':0},c,10)
    assert not conditions


def test_unreachable_requires_valid_rounds_and_recovery_window():
    c=ProbeConfig().model_dump();m={'warm':False,'coverage':.3,'p95_ms':None,'loss_percent':100}
    conditions,counters={},{}
    for _ in range(2):conditions,counters=transition(conditions,counters,m,c,0)
    assert not conditions
    conditions,counters=transition(conditions,counters,m,c,0)
    assert conditions['unreachable']=='critical'
    for _ in range(3):conditions,counters=transition(conditions,counters,{**m,'p95_ms':12,'loss_percent':0},c,10)
    assert conditions=={'recovery_pending':'warning'}
    for _ in range(3):conditions,counters=transition(conditions,counters,{**m,'warm':True,'coverage':1,'p95_ms':12,'loss_percent':0},c,10)
    assert not conditions


def test_incident_lifecycle_invalid_and_maintenance():
    with session() as db:
        t=target(db);start=now()//300*300-1800
        s=ProbeSeries(target_id=t.id,address=t.address,epoch=t.epoch,started_at=start,config=t.effective)
        db.add(s);db.commit()
        for i in range(3):r=add_round(db,t,s,start+i*30,0)
        incident=db.scalar(select(ProbeIncident));assert incident and incident.closed_at is None
        evaluate(db,t,s,r);db.commit()
        assert db.scalar(select(func.count()).select_from(ProbeIncidentEvent))==1
        add_round(db,t,s,start+90,0,status='invalid')
        add_round(db,t,s,start+120,10,maintenance=True)
        db.refresh(incident);assert incident.closed_at is None
        for i in range(5,18):add_round(db,t,s,start+i*30,10)
        db.refresh(incident);assert incident.closed_at is not None
        assert db.scalar(select(func.count()).select_from(ProbeIncident))==1
        assert db.scalar(select(ProbeIncidentEvent).where(ProbeIncidentEvent.kind=='recovered'))


def test_scheduler_fencing_no_catchup_and_no_duplicate(monkeypatch):
    with session() as db:
        t=target(db);stamp=slot_at(t,now());t.next_due=stamp-300;db.commit()
        assert claim(db,stamp+8)=={'skipped':True}
        assert db.scalar(select(func.count()).select_from(ProbeBatch))==0
        task=claim(db,stamp+30);assert task
        assert claim(db,stamp+30) is None
        # A settings change invalidates the in-flight result.
        t=db.get(ProbeTarget,t.id);changed(db,t);db.commit()
    monkeypatch.setattr('odin.probes.runner.resolve',lambda *_:['10.20.0.2'])
    monkeypatch.setattr('odin.probes.runner.fping.run',lambda *_:{'10.20.0.2':{'sent':10,'received':0,'samples':[None]*10}})
    execute(task)
    with session() as db:
        assert db.get(ProbeBatch,task['id']).status=='cancelled'
        assert db.scalar(select(func.count()).select_from(ProbeRound))==0


def test_restart_expired_batch_not_replayed():
    with session() as db:
        t=target(db);stamp=slot_at(t,now());first=claim(db,stamp)
        t.next_due=stamp+30;db.commit()
        later=claim(db,stamp+120)
        assert later and later['id']!=first['id']
        db.expire_all();assert db.get(ProbeBatch,first['id']).error_code=='probe_interrupted'
        assert db.scalar(select(func.count()).select_from(ProbeBatch))==2


def test_dns_change_rejected_before_fping(monkeypatch):
    with session() as db:
        t=target(db);stamp=slot_at(t,now());task=claim(db,stamp)
    ping=Mock();monkeypatch.setattr('odin.probes.runner.fping.run',ping)
    monkeypatch.setattr('odin.probes.runner.resolve',lambda *_:['10.20.0.3'])
    execute(task)
    assert not ping.called
    with session() as db:
        assert db.get(ProbeBatch,task['id']).error_code=='dns_approval_required'
        assert db.scalar(select(func.count()).select_from(ProbeIncident))==0


def test_manual_probe_does_not_affect_detector(monkeypatch):
    with session() as db:
        t=target(db);t.next_due=now()+60
        db.add(ProbeBatch(target_id=t.id,epoch=t.epoch,slot=now(),mode='manual',config=t.effective));db.commit()
        task=claim(db)
    monkeypatch.setattr('odin.probes.runner.resolve',lambda *_:['10.20.0.2'])
    monkeypatch.setattr('odin.probes.runner.fping.run',lambda *_:{'10.20.0.2':{'sent':10,'received':0,'samples':[None]*10}})
    execute(task);execute(task)
    with session() as db:
        assert db.get(ProbeBatch,task['id']).result['rounds'][0]['received']==0
        assert db.scalar(select(func.count()).select_from(ProbeRound))==0
        assert db.scalar(select(func.count()).select_from(ProbeIncident))==0


def test_rollups_preserve_weighted_samples_and_missing_coverage():
    with session() as db:
        t=target(db);start=now()//3600*3600-3600
        s=ProbeSeries(target_id=t.id,address=t.address,epoch=t.epoch,started_at=start,config=t.effective)
        db.add(s);db.commit()
        add_round(db,t,s,start,10,value=10);add_round(db,t,s,start+30,1,value=100)
        aggregate_bucket(db,start);aggregate_hour(db,start);db.commit()
        rows=list(db.scalars(select(ProbeRollup)))
        assert len(rows)==2
        for r in rows:
            assert r.sent==20 and r.received==11 and r.rtt_sum==200 and r.expected==10 and r.valid==2
        aggregate_bucket(db,start);db.commit()
        assert db.scalar(select(func.count()).select_from(ProbeRollup))==2
        upkeep(db,now())


def test_upkeep_purges_scheduled_batches_sooner_than_manual_runs():
    with session() as db:
        t=target(db);stamp=now()
        def batch(mode,age,status='done'):
            row=ProbeBatch(target_id=t.id,epoch=t.epoch,slot=stamp-age,mode=mode,status=status,
                finished_at=None if status=='running' else stamp-age,config={})
            db.add(row);db.flush();return row.id
        kept={batch('scheduled',86400),batch('manual',3*86400),batch('scheduled',20*86400,'running')}
        batch('scheduled',3*86400);batch('manual',15*86400);db.commit()
        upkeep(db,stamp)
        assert set(db.scalars(select(ProbeBatch.id)))==kept


def test_claim_queries_have_indexes():
    with session() as db:
        names=set(db.scalars(text("SELECT indexname FROM pg_indexes WHERE tablename='probe_batches'")))
    assert {'ix_probe_batches_active','ix_probe_batches_finished'}<=names


def test_stale_probe_never_looks_healthy():
    with session() as db:
        t=target(db);stamp=now();db.add(ProbeOrigin(id='odin',heartbeat=stamp-90))
        s=ProbeSeries(target_id=t.id,address=t.address,epoch=t.epoch,started_at=stamp-600,config=t.effective)
        db.add(s);db.flush()
        db.add(ProbeState(target_id=t.id,address=t.address,series_id=s.id,last_slot=stamp,metrics={'state':'healthy'},conditions={},counters={}))
        db.commit()
        assert target_views(db,[t])[0]['state']=='no_data'
        db.get(ProbeOrigin,'odin').heartbeat=stamp
        db.scalar(select(ProbeState)).metrics={'state':'maintenance'}
        db.commit()
        assert target_views(db,[t])[0]['state']=='warming'


def test_target_api_versioning_profiles_and_permissions(client,administrator):
    enroll(client)
    body={'name':'PE-PRUEBA','address':'10.20.0.2','enabled':True}
    response=client.post('/api/v1/targets',json=body);assert response.status_code==201,response.text
    t=response.json();assert t['effective']['rtt_warning'] is None
    assert client.put('/api/v1/targets/'+t['id'],json={**body,'version':99}).status_code==409
    assert client.post('/api/v1/targets',json={**body,'address':'127.0.0.1'}).status_code==422
    assert client.post('/api/v1/targets',json={'name':'DNS','address':'router.example.com'}).status_code==422
    p=client.get('/api/v1/probe-profiles').json()[0]
    result=client.put('/api/v1/probe-profiles/'+p['id'],json={'name':p['name'],'version':p['version'],
        'config':{**p['versions'][0]['config'],'rtt_warning':30,'rtt_critical':60},'apply_to_targets':True})
    assert result.status_code==200,result.text
    updated=client.get('/api/v1/targets').json()[0]
    assert updated['version']==2 and updated['effective']['rtt_warning']==30
    run1=client.post('/api/v1/targets/'+t['id']+'/probe-runs');assert run1.status_code==202,run1.text
    assert client.post('/api/v1/targets/'+t['id']+'/probe-runs').json()['id']==run1.json()['id']
    with session() as db:
        from odin.models import User
        db.get(User,administrator).role='operator';db.commit()
    assert client.get('/api/v1/targets').status_code==200
    assert client.post('/api/v1/targets',json={**body,'name':'PE-OTRO'}).status_code==403


def test_maintenance_history_ack_and_pop_archival(client,administrator):
    enroll(client)
    pop=client.post('/api/v1/pops',json={'name':'POP-LAB'}).json()
    t=client.post('/api/v1/targets',json={'name':'PE-LAB','address':'10.20.0.2','pop_id':pop['id']}).json()
    assert client.post('/api/v1/pops/'+pop['id']+'/archive',json={'version':pop['version']}).status_code==409
    result=client.post('/api/v1/targets/'+t['id']+'/maintenance',json={'starts_at':now(),'ends_at':now()+600,'reason':'Prueba'})
    assert result.status_code==201,result.text
    assert client.post('/api/v1/maintenance/'+result.json()['id']+'/cancel').status_code==200
    with session() as db:
        incident=ProbeIncident(target_id=t['id'],address=t['address'],severity='critical',conditions={'unreachable':'critical'},profile=t['effective'])
        db.add(incident);db.commit();key=incident.id
    for _ in range(2):assert client.post('/api/v1/incidents/'+key+'/acknowledgments').status_code==200
    assert client.post('/api/v1/incidents/'+key+'/observations',json={'text':'Revisión del enlace'}).status_code==201
    detail=client.get('/api/v1/incidents/'+key).json()
    assert sum(e['kind']=='acknowledged' for e in detail['events'])==1
    assert detail['closed_at'] is None
    assert client.get('/api/v1/probe-overview').json()['incidents']==1
    assert client.post('/api/v1/targets/'+t['id']+'/archive',json={'version':t['version']}).status_code==200
    assert client.get('/api/v1/incidents/'+key).status_code==200


def test_partial_coverage_can_evaluate_without_inventing_loss():
    with session() as db:
        t=target(db);start=now()//300*300-1800
        s=ProbeSeries(target_id=t.id,address=t.address,epoch=t.epoch,started_at=start,config=t.effective)
        db.add(s);db.commit()
        for i in range(11):
            add_round(db,t,s,start+i*30,9,status='invalid' if i in (2,5) else 'valid')
        state=db.scalar(select(ProbeState))
        assert state.metrics['coverage']==.8
        assert state.metrics['loss_percent']==10
        assert state.conditions=={'loss':'warning'}


def test_import_pe_is_idempotent_and_preserves_existing(client,administrator):
    from odin.models import Device,SourceIdentity,Integration
    from odin.probes.seed import import_pe
    with session() as db:
        db.add(Integration(kind='librenms',endpoint='https://lab.example.test'));db.flush()
        d=Device(name='PE-LAB');other=Device(name='CPE-LAB');db.add_all([d,other]);db.flush()
        for device,ip in [(d,'10.20.0.2'),(other,'10.20.0.3')]:
            db.add(SourceIdentity(source='librenms',external_id=device.id,device_id=device.id,name=device.name,addresses=[ip],last_seen=now()))
        db.commit()
        assert len(import_pe(db,False))==1
        assert db.scalar(select(func.count()).select_from(ProbeTarget))==0
        assert import_pe(db,True)[0]['status']=='creado'
        assert import_pe(db,True)[0]['status']=='ya_registrado'
        assert db.scalar(select(func.count()).select_from(ProbeTarget))==1


def test_category_catalog_and_metadata_preserve_measurements(client,administrator):
    assert client.get('/api/v1/target-categories').status_code==401
    enroll(client)
    catalog=client.get('/api/v1/target-categories').json()
    assert len(catalog)==16
    assert {c['id'] for c in catalog if 'WhatsApp' in c['services']}=={'social','mensajeria'}
    body={'name':'WhatsApp prueba','address':'10.20.0.9','categories':['social','mensajeria','social'],'service':'WhatsApp'}
    created=client.post('/api/v1/targets',json=body)
    assert created.status_code==201,created.text
    t=created.json();assert t['categories']==['social','mensajeria'] and t['service']=='WhatsApp'
    updated=client.put('/api/v1/targets/'+t['id'],json={**body,'categories':['mensajeria'],'version':t['version']})
    assert updated.status_code==200,updated.text
    u=updated.json();assert u['epoch']==t['epoch'] and u['effective']==t['effective']
    # Older clients must not erase classification when they omit the new fields.
    omitted=client.put('/api/v1/targets/'+t['id'],json={'name':t['name'],'address':t['address'],'version':u['version']})
    assert omitted.json()['categories']==['mensajeria'] and omitted.json()['service']=='WhatsApp'
    assert client.post('/api/v1/targets',json={**body,'categories':['inventada']}).status_code==422
    with session() as db:
        from odin.models import User
        db.get(User,administrator).role='operator';db.commit()
    assert client.get('/api/v1/target-categories').status_code==200
    assert client.put('/api/v1/targets/'+t['id'],json={**body,'version':omitted.json()['version']}).status_code==403
