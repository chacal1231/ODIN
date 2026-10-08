import pytest
from sqlalchemy import select,func
from odin.db import session
from odin.models import (MonitoringDestination, ProbeTarget, ProbeSeries, ProbeState, ProbeOrigin,
    ProbeIncident, Device, Pop, InventoryLink, User, now)
from odin.probes.organization import aggregate,overview,ensure_destination
from odin.probes.config import TRANSPORT_VERSION_ID
from test_access import enroll
from test_probes import target


@pytest.mark.parametrize('states,expected',[
    ([], 'unmonitored'),(['healthy'],'healthy'),(['healthy','critical'],'partial'),
    (['critical','critical'],'critical'),(['healthy','no_data'],'no_data'),
    (['healthy','pending_validation'],'pending_validation'),(['healthy','warming'],'warming'),
    (['healthy','paused'],'paused'),(['no_data','critical'],'partial')])
def test_aggregate_preserves_unknown_and_partial(states,expected):
    assert aggregate([{'state':s} for s in states])==expected


def test_multiple_checks_share_destination_and_keep_epochs(client,administrator):
    assert client.get('/api/v1/monitoring').status_code==401
    enroll(client)
    body={'name':'AWS-LAB','address':'1.1.1.1','categories':['hyperscalers'],'service':'AWS','enabled':False}
    first=client.post('/api/v1/targets',json=body)
    assert first.status_code==201,first.text
    a=first.json()
    second=client.post('/api/v1/targets',json={**body,'name':'AWS-LAB HTTPS','destination_id':a['destination_id'],
        'protocol':'https','profile_version_id':TRANSPORT_VERSION_ID,'essential':False})
    assert second.status_code==201,second.text
    b=second.json()
    grouped=client.get('/api/v1/monitoring').json()
    assert grouped['counts']['destinations']==1 and grouped['counts']['checks']==2
    d=grouped['destinations'][0]
    assert d['check_count']==2 and d['scope']=='external'
    rename={'name':'AWS Ashburn','scope':'external','version':d['version']}
    url='/api/v1/monitoring/destinations/'+d['id']
    assert client.put(url,json=rename).status_code==200
    assert client.put(url,json=rename).status_code==409
    with session() as db:
        assert db.get(ProbeTarget,a['id']).epoch==a['epoch']
        assert db.get(ProbeTarget,b['id']).epoch==b['epoch']
        db.get(User,administrator).role='operator';db.commit()
    assert client.get('/api/v1/monitoring').status_code==200
    assert client.put(url,json={**rename,'version':2}).status_code==403


def test_optional_failure_does_not_override_required_check():
    with session() as db:
        a=target(db);entity=ensure_destination(db,a)
        b=ProbeTarget(name='Optional HTTPS',address='10.20.0.2',family='ipv4',destination_id=entity.id,
            protocol='https',essential=False,profile_version_id=TRANSPORT_VERSION_ID,approved_ips=['10.20.0.2'],
            effective={**a.effective,'packets':1},enabled=True)
        db.add(b);db.add(ProbeOrigin(id='odin',heartbeat=now()));db.flush()
        for t,state in [(a,'healthy'),(b,'critical')]:
            series=ProbeSeries(target_id=t.id,address=t.address,epoch=t.epoch,started_at=now()-600,config=t.effective)
            db.add(series);db.flush()
            db.add(ProbeState(target_id=t.id,address=t.address,series_id=series.id,last_slot=now(),metrics={'state':state},conditions={},counters={}))
        db.commit()
        d=overview(db)['destinations'][0]
        assert d['state']=='healthy' and d['optional_issues']==1
        b.essential=True;db.commit()
        assert overview(db)['destinations'][0]['state']=='partial'


def test_device_pop_moves_and_link_state_is_independent(client,administrator):
    enroll(client)
    with session() as db:
        p=Pop(name='Norte');q=Pop(name='Sur');db.add_all([p,q]);db.flush()
        device=Device(name='PE-LAB',pop_id=p.id);other=Device(name='PE-OTRO',pop_id=q.id);db.add_all([device,other]);db.flush()
        link=InventoryLink(left_id=device.id,right_id=other.id,actor_id=administrator);db.add(link);db.commit()
        t=target(db,categories=['red_propia']);entity=ensure_destination(db,t);db.commit()
        info=overview(db);assert info['destinations'][0]['pop_id']==p.id
        assert info['links'][0]['state']=='unmonitored'
        device.pop_id=q.id;db.commit()
        assert overview(db)['destinations'][0]['pop_id']==q.id
        ids=(entity.id,device.id,link.id)
    url='/api/v1/monitoring/destinations/'+ids[0]
    body={'name':'Link lab','scope':'network','version':1,'device_id':ids[1],'link_id':ids[2]}
    assert client.put(url,json=body).status_code==422
    assert client.put(url,json={**body,'device_id':None}).status_code==200
    assert client.get('/api/v1/monitoring').json()['links'][0]['destinations']==[ids[0]]


def test_grouped_incidents_retain_individual_evidence():
    with session() as db:
        p=Pop(name='Norte');db.add(p);db.flush()
        t=target(db,categories=['red_propia'],pop_id=p.id);ensure_destination(db,t)
        for ip in ['10.20.0.2','10.20.0.3']:
            db.add(ProbeIncident(target_id=t.id,address=ip,pop_id=p.id,severity='critical',conditions={'unreachable':'critical'},profile=t.effective))
        db.commit();data=overview(db)
        assert len(data['incident_groups'])==1
        assert data['incident_groups'][0]['name']=='Norte'
        assert len(data['incident_groups'][0]['items'])==2
        assert db.scalar(select(func.count()).select_from(ProbeIncident))==2
        t.archived=True;db.commit()
        assert len(overview(db)['incident_groups'][0]['items'])==2


def test_invalid_destination_does_not_create_or_move_checks(client,administrator):
    enroll(client)
    body={'name':'PE-LAB','address':'10.20.0.2'}
    assert client.post('/api/v1/targets',json={**body,'destination_id':'missing'}).status_code==422
    created=client.post('/api/v1/targets',json=body).json()
    assert client.put('/api/v1/targets/'+created['id'],json={**body,'version':created['version'],'destination_id':None}).status_code==422
    assert client.post('/api/v1/targets',json=body).status_code==409
