from concurrent.futures import ThreadPoolExecutor
import pytest
from pydantic import ValidationError
from sqlalchemy import select,func
from odin.db import session
from odin.models import EventStream,EventPage,SourceEvent,Job,Integration,SourceIdentity,Device,now
from odin.events.contracts import EventRecord,EventPageInput
from odin.events.service import ingest_page,IngestionConflict,execute_index
from odin.jobs import claim
from test_access import enroll


def event(key='e1',**changes):
    return EventRecord(**(dict(external_id=key,entity_id='host1',occurred_at=100,source_updated_at=100,
        kind='problem',status='open',severity='warning',original_severity='3',title='Latencia alta',reference=key) | changes))


def page(key='p1',version=0,events=None,**changes):
    return EventPageInput(**(dict(request_key=key,expected_version=version,window_start=0,window_end=200,
        coverage='complete',events=[event()] if events is None else events) | changes))


def save(value,source='zabbix'):
    with session() as db:
        result=ingest_page(db,source,'events',value);db.commit();return result


def count(db,model):return db.scalar(select(func.count()).select_from(model))


def test_page_replay_atomic_outbox_and_resume():
    p=page(coverage='partial',reason='pagination',next_cursor='page2')
    first=save(p)
    assert save(p)=={**first,'replayed':True}
    with session() as db:
        assert count(db,SourceEvent)==count(db,EventPage)==count(db,Job)==1
        stream=db.scalar(select(EventStream))
        assert stream.cursor=='page2' and stream.version==1 and stream.complete_through is None
    save(page('p2',1,[event('e2')],cursor='page2'))
    with session() as db:
        stream=db.scalar(select(EventStream))
        assert stream.coverage=='complete' and stream.complete_through==200 and stream.cursor is None
        assert count(db,SourceEvent)==count(db,EventPage)==count(db,Job)==2


def test_rollback_leaves_no_events_checkpoint_or_job():
    with session() as db:
        ingest_page(db,'zabbix','events',page());db.rollback()
    with session() as db:
        assert all(count(db,m)==0 for m in [SourceEvent,EventPage,EventStream,Job])
    assert save(page())['changed']==1


def test_late_revisions_cannot_reopen_recovered_event():
    save(page())
    save(page('p2',1,[event(status='recovered',source_updated_at=150)]))
    assert save(page('p3',2,[event()]))['changed']==0
    with session() as db:
        e=db.scalar(select(SourceEvent))
        assert e.status=='recovered' and e.source_updated_at==150 and e.occurred_at==100
        assert count(db,SourceEvent)==1 and count(db,Job)==2


def test_withdrawn_revision_is_stored_and_older_reads_cannot_reopen_it():
    save(page())
    save(page('p2',1,[event(status='withdrawn',source_updated_at=150)]))
    assert save(page('p3',2,[event()]))['changed']==0
    with session() as db:
        assert db.scalar(select(SourceEvent)).status=='withdrawn'


def test_same_timestamp_conflict_rolls_back_entire_page_even_if_caught():
    save(page())
    with session() as db:
        with pytest.raises(IngestionConflict) as error:
            ingest_page(db,'zabbix','events',page('p2',1,[event('new'),event(title='contradiction')]))
        assert error.value.code=='revision_conflict'
        db.commit()
    with session() as db:
        assert count(db,SourceEvent)==1 and count(db,EventPage)==1 and count(db,Job)==1
        assert db.scalar(select(EventStream)).version==1


def test_duplicate_request_key_and_stale_cursor_rejected():
    save(page())
    with pytest.raises(IngestionConflict,check=lambda e:e.code=='page_key_reused'):save(page(events=[event('other')]))
    with pytest.raises(IngestionConflict,check=lambda e:e.code=='checkpoint_stale'):save(page('p2',0))
    with pytest.raises(IngestionConflict):save(page('p2',1,cursor='unknown'))


def test_failed_read_preserves_progress_and_empty_success_is_distinct():
    save(page(coverage='partial',reason='pagination',next_cursor='page2'))
    save(page('failure',1,[],coverage='unavailable',reason='timeout',cursor='page2',next_cursor='page2'))
    with session() as db:
        s=db.scalar(select(EventStream))
        assert s.coverage=='unavailable' and s.cursor=='page2' and s.complete_through is None
    save(page('empty',2,[],cursor='page2'))
    with session() as db:
        s=db.scalar(select(EventStream))
        assert s.coverage=='complete' and s.complete_through==200 and s.cursor is None
        assert count(db,SourceEvent)==1 and count(db,Job)==1


def test_sources_and_recovery_references_are_isolated():
    save(page());save(page(),source='librenms')
    save(page('recovery',1,[event('r1',status='recovered',recovery_of='e1',occurred_at=150,source_updated_at=150)]))
    with session() as db:
        assert count(db,SourceEvent)==3
        # Merely storing evidence must not mutate a different event or source.
        assert len(list(db.scalars(select(SourceEvent).where(SourceEvent.status=='open'))))==2


def test_concurrent_page_retry_creates_only_one_receipt():
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:save(page()),range(2)))
    assert sorted(r['replayed'] for r in results)==[False,True]
    with session() as db:
        assert count(db,SourceEvent)==count(db,Job)==count(db,EventPage)==1


def test_index_job_requires_live_lease_and_matches_exact_identity():
    with session() as db:
        db.add(Integration(kind='zabbix',endpoint='https://example.test'));device=Device(name='router');db.add(device);db.flush()
        db.add(SourceIdentity(source='zabbix',external_id='host1',device_id=device.id,name='router',last_seen=now()));db.commit();device_id=device.id
    save(page())
    with session() as db:job=claim(db)
    assert execute_index(job.id,'wrong') is False
    assert execute_index(job.id,job.lease_token) is True
    assert execute_index(job.id,job.lease_token) is False
    with session() as db:
        assert db.scalar(select(SourceEvent)).device_id==device_id
        assert db.get(Job,job.id).status=='done'


def test_completeness_does_not_bridge_missing_window():
    save(page())
    save(page('p2',1,[],window_start=300,window_end=400))
    with session() as db:assert db.scalar(select(EventStream)).complete_through==200


@pytest.mark.parametrize('changes',[
    {'coverage':'unavailable','events':[event()],'reason':'timeout'},
    {'coverage':'partial'}, {'events':[event(),event()]},
    {'window_start':300,'window_end':100}, {'expected_version':-1},
    {'coverage':'unsupported','reason':'unsupported','events':[],'next_cursor':'skip'},
])
def test_invalid_contracts(changes):
    with pytest.raises(ValidationError):page(**changes)


def test_api_auth_filters_pagination_and_no_cursor_disclosure(client,administrator):
    for path in ['/api/v1/source-events','/api/v1/event-streams','/api/v1/event-streams/missing/pages']:
        assert client.get(path).status_code==401
    save(page(events=[event(str(i)) for i in range(5)]))
    save(page('p2',1,[],coverage='partial',reason='pagination',next_cursor='opaque-private-cursor'))
    enroll(client)
    ids=[];cursor=None
    for _ in range(3):
        params={'limit':2}
        if cursor:params['cursor']=cursor
        response=client.get('/api/v1/source-events',params=params)
        assert response.status_code==200,response.text
        body=response.json();ids.extend(e['id'] for e in body['items']);cursor=body['next_cursor']
    assert len(ids)==len(set(ids))==5 and cursor is None
    assert not client.get('/api/v1/source-events?source=librenms').json()['items']
    assert not client.get('/api/v1/source-events?start=101').json()['items']
    assert client.get('/api/v1/source-events?cursor=invalid').status_code==422
    assert client.get('/api/v1/source-events?start='+str(10**30)).status_code==422
    streams=client.get('/api/v1/event-streams').json()
    assert 'opaque-private-cursor' not in str(streams)
    history=client.get('/api/v1/event-streams/'+streams['streams'][0]['id']+'/pages').json()
    assert len(history['items'])==2
    assert 'content_hash' not in history['items'][0] and 'request_key' not in history['items'][0]
    from odin.models import User
    with session() as db:db.get(User,administrator).role='operator';db.commit()
    assert client.get('/api/v1/source-events').status_code==200
    assert client.post('/api/v1/source-events',json={}).status_code==405
