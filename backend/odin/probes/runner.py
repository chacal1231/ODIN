import logging
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import select
from ..db import session
from ..config import settings
from ..models import ProbeTarget, ProbeBatch, ProbeSeries, ProbeRound, ProbeOrigin, now
from . import fping, transport
from .resolver import resolve, resolve_task, ProbeError
from .service import claim, maintenance_at
from .detector import evaluate
from .storage import upkeep
from .config import seed_defaults

# Stable identity within a target/configuration epoch; never sent to the network.
SERVICE_ADDRESS = 'Servicio'


def service_result(addresses, results, protocol):
    """One scheduled observation, retaining every IP attempt for diagnostics."""
    if not addresses or set(results) != set(addresses):
        return None
    return {
        'sent': sum(r['sent'] for r in results.values()),
        'received': sum(r['received'] for r in results.values()),
        'samples': [sample for r in results.values() for sample in r['samples']],
        'details': {
            'protocol': protocol,
            'addresses': addresses,
            'rounds': [{'address': ip, **results[ip]} for ip in addresses],
            'checks': [{'address': ip, **check} for ip in addresses
                       for check in results[ip].get('details', {}).get('checks', [])],
        },
    }


stop=threading.Event()


def execute(task):
    addresses=None;results={};error=None
    try:
        if task.get('dns_policy')=='public_dns':addresses=resolve_task(task)
        else:
            addresses=resolve(task['address'],task['family'])
            if not set(addresses).issubset(task['approved_ips']):raise ProbeError('dns_approval_required')
        results=fping.run(addresses,task['config']) if task['config'].get('protocol','icmp')=='icmp' else transport.run(addresses,task['address'],task['config'])
    except ProbeError as exc:error=exc.code
    except Exception:error='probe_execution_failed'
    with session() as db:
        target=db.scalar(select(ProbeTarget).where(ProbeTarget.id==task['target_id']).with_for_update())
        batch=db.scalar(select(ProbeBatch).where(ProbeBatch.id==task['id']).with_for_update())
        if not batch or batch.status!='running' or batch.token!=task['token'] or batch.lease_until<now():return
        if target.epoch!=task['epoch'] or target.lease_token!=task['token'] or target.archived:
            batch.status='cancelled';batch.error_code='configuration_changed';batch.finished_at=now();db.commit();return
        target.lease_token=None;target.lease_until=None
        batch.finished_at=now();batch.error_code=error;batch.status='failed' if error else 'done'
        batch.result={'addresses':addresses or [],'error_code':error,'protocol':task['config'].get('protocol','icmp'),
            'rounds':[{'address':ip,**data} for ip,data in results.items()]}
        if batch.mode=='manual':
            batch.result={**batch.result,'rounds':[{'address':ip,**data} for ip,data in results.items()]}
            db.commit();return
        target.last_error=error
        if addresses is not None:target.resolved_ips=addresses;target.resolved_at=now()
        active={s.address:s for s in db.scalars(select(ProbeSeries).where(ProbeSeries.target_id==target.id,ProbeSeries.ended_at.is_(None)))}
        logical_service=task['config'].get('protocol','icmp') in ('tcp','https')
        series_addresses=[SERVICE_ADDRESS] if logical_service else addresses
        if series_addresses is not None and (logical_service or error!='dns_approval_required'):
            for ip,series in list(active.items()):
                if ip not in series_addresses:series.ended_at=batch.slot;del active[ip]
            for ip in series_addresses:
                if ip not in active:
                    series=ProbeSeries(target_id=target.id,address=ip,epoch=target.epoch,started_at=batch.slot,config=batch.config)
                    db.add(series);db.flush();active[ip]=series
        maintained=maintenance_at(db,target.id,batch.slot)
        for ip,series in active.items():
            data=service_result(addresses,results,task['config']['protocol']) if logical_service else results.get(ip)
            row=ProbeRound(series_id=series.id,slot=batch.slot,batch_id=batch.id,
                status='valid' if data and not error else 'invalid',sent=data['sent'] if data else 0,
                received=data['received'] if data else 0,samples=data['samples'] if data else [],
                error_code=error,details=data.get('details',{}) if data else {},finished_at=batch.finished_at)
            db.add(row);db.flush()
            evaluate(db,target,series,row,maintained)
        db.commit()


def heartbeat(error,version):
    with session() as db:
        origin=db.get(ProbeOrigin,'odin')
        if not origin:origin=ProbeOrigin(id='odin',heartbeat=now());db.add(origin)
        origin.heartbeat=now();origin.error_code=error;origin.fping_version=version
        db.commit()


def main():
    logging.basicConfig(level=logging.INFO)
    logging.getLogger('httpx').setLevel(logging.WARNING)  # request URLs may carry secrets (Telegram's bot token)
    signal.signal(signal.SIGTERM,lambda *_:stop.set());signal.signal(signal.SIGINT,lambda *_:stop.set())
    with session() as db:seed_defaults(db)
    with ThreadPoolExecutor(max_workers=settings().probe_concurrency) as pool:
        futures=set();last_upkeep=0
        while not stop.is_set():
            try:
                version=fping.version()
                heartbeat(None,version)
                if now()-last_upkeep>60:
                    with session() as db:upkeep(db,now())
                    last_upkeep=now()
                finished={future for future in futures if future.done()}
                for future in finished:
                    try:future.result()
                    except Exception:logging.error('probe_result_persistence_failed')
                futures-=finished
                # Limited scans skip stale slots without replaying them.
                for _ in range(settings().probe_max_addresses+1):
                    if len(futures)>=settings().probe_concurrency:break
                    with session() as db:task=claim(db)
                    if task and task.get('skipped'):continue
                    if task:futures.add(pool.submit(execute,task))
                    else:break
            except ProbeError as exc:
                logging.error('%s',exc.code)
                try:heartbeat(exc.code,None)
                except Exception:pass
            except Exception:
                logging.error('probe_cycle_failed')
                try:heartbeat('probe_cycle_failed',None)
                except Exception:pass
            stop.wait(1)


if __name__=='__main__':main()
