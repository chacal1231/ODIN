"""Service windows survive DNS rotation without counting IPs as extra rounds."""
import pytest
from sqlalchemy import select
from odin.db import session
from odin.models import ProbeTarget, ProbeSeries, ProbeRound, ProbeState, ProbeIncident, now
from odin.probes.config import ProbeConfig
from odin.probes.runner import execute, SERVICE_ADDRESS
from odin.probes.service import claim, slot_at, changed
from odin.probes.resolver import ProbeError
from test_probes import target


def setup_target(protocol):
    with session() as db:
        t=target(db, protocol=protocol, dns_policy='public_dns')
        t.address='cloud.example.com'
        t.effective=ProbeConfig(interval_seconds=60,packets=1).model_dump()
        t.check_config={'port':443}
        start=slot_at(t,now())
        t.next_due=start
        db.commit()
        return t.id,start


def run_round(monkeypatch, target_id, slot, addresses, replies=True, error=None):
    def resolve(_):
        if error:raise ProbeError(error)
        return addresses
    monkeypatch.setattr('odin.probes.runner.resolve_task',resolve)
    monkeypatch.setattr('odin.probes.runner.transport.run',lambda ips,*_: {
        ip:{'sent':1,'received':int(replies),'samples':[12 if replies else None],
            'details':{'checks':[{'status':'ok' if replies else 'failed'}]}} for ip in ips})
    with session() as db:
        task=claim(db,slot)
    assert task and task['target_id']==target_id
    execute(task)


@pytest.mark.parametrize('protocol',['tcp','https'])
def test_rotation_keeps_one_window_and_ip_evidence(monkeypatch,protocol):
    key,start=setup_target(protocol)
    for i in range(5):
        run_round(monkeypatch,key,start+60*i,[f'8.8.8.{i+1}','1.1.1.1'])
        with session() as db:
            state=db.scalar(select(ProbeState))
            assert state.metrics['valid_rounds']==i+1
            assert state.metrics['coverage']==(i+1)/5
            assert state.metrics['state']==('healthy' if i==4 else 'warming')
    with session() as db:
        series=list(db.scalars(select(ProbeSeries)))
        assert len(series)==1 and series[0].address==SERVICE_ADDRESS
        rounds=list(db.scalars(select(ProbeRound).order_by(ProbeRound.slot)))
        assert len(rounds)==5
        assert rounds[0].details['addresses']==['8.8.8.1','1.1.1.1']
        assert rounds[-1].details['rounds'][0]['address']=='8.8.8.5'
        assert state.metrics['sent']==10


def test_rotating_failures_open_and_recover_same_incident(monkeypatch):
    key,start=setup_target('tcp')
    for i in range(3):run_round(monkeypatch,key,start+60*i,[f'8.8.8.{i+1}'],False)
    with session() as db:
        incident=db.scalar(select(ProbeIncident))
        assert incident.address==SERVICE_ADDRESS and incident.closed_at is None
    for i in range(3,14):run_round(monkeypatch,key,start+60*i,[f'8.8.8.{i+1}'])
    with session() as db:
        incidents=list(db.scalars(select(ProbeIncident)))
        assert len(incidents)==1 and incidents[0].closed_at is not None
        assert db.scalar(select(ProbeState)).metrics['state']=='healthy'


def test_dns_errors_do_not_count_as_samples_or_failures(monkeypatch):
    key,start=setup_target('https')
    for i in range(5):
        run_round(monkeypatch,key,start+60*i,['8.8.8.8'],error='dns_timeout' if i in (1,2) else None)
    with session() as db:
        state=db.scalar(select(ProbeState))
        assert state.metrics['valid_rounds']==3 and state.metrics['coverage']==.6
        assert state.metrics['state']=='warming'
        assert state.metrics['failure_percent']==0
        assert db.scalar(select(ProbeIncident)) is None


def test_configuration_change_restarts_service_window(monkeypatch):
    key,start=setup_target('tcp')
    for i in range(5):run_round(monkeypatch,key,start+60*i,['8.8.8.8'])
    with session() as db:
        t=db.get(ProbeTarget,key);changed(db,t);t.next_due=start+300;db.commit()
    run_round(monkeypatch,key,start+300,['1.1.1.1'])
    with session() as db:
        state=db.scalar(select(ProbeState))
        assert state.metrics['state']=='warming' and state.metrics['valid_rounds']==1
        assert len(list(db.scalars(select(ProbeSeries))))==2


def test_existing_ip_history_is_retained(monkeypatch):
    key,start=setup_target('https')
    with session() as db:
        t=db.get(ProbeTarget,key)
        old=ProbeSeries(target_id=key,address='8.8.8.8',epoch=t.epoch,started_at=start-60,config=t.effective)
        db.add(old);db.commit();old_id=old.id
    run_round(monkeypatch,key,start,['1.1.1.1'])
    with session() as db:
        assert db.get(ProbeSeries,old_id).ended_at==start
        assert db.scalar(select(ProbeState)).address==SERVICE_ADDRESS
