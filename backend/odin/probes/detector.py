"""Deterministic detection; missing measurements never mean packet loss."""
import math
from copy import deepcopy
from sqlalchemy import select
from ..models import ProbeRound, ProbeState, ProbeIncident, ProbeIncidentEvent, now
from . import trace


def window_metrics(rounds, config, slot, warm_since):
    interval=config['interval_seconds']
    expected=config['window_seconds']//interval
    valid=[r for r in rounds if r.status=='valid' and slot-config['window_seconds']<r.slot<=slot and r.slot>=warm_since]
    samples=sorted(value for r in valid for value in r.samples if value is not None)
    sent=sum(r.sent for r in valid);received=sum(r.received for r in valid)
    protocol=config.get('protocol','icmp')
    percentage=100*(sent-received)/sent if sent else None
    return {'protocol':protocol,'failure_percent':percentage if protocol!='icmp' else None,'p95_ms':samples[math.ceil(len(samples)*0.95)-1] if samples else None,
            'loss_percent':percentage if protocol=='icmp' else None,
            'sent':sent,'received':received,'valid_rounds':len(valid),'expected_rounds':expected,
            'coverage':min(1,len(valid)/expected),'window_seconds':config['window_seconds'],
            'warm':slot-warm_since+interval>=config['window_seconds']}


def transition(conditions, counters, metrics, config, received):
    conditions=deepcopy(conditions);counters=deepcopy(counters)
    unreachable='unreachable' if config.get('protocol','icmp')=='icmp' else 'endpoint_unavailable'
    counters['no_reply']=counters.get('no_reply',0)+1 if received==0 else 0
    counters['reply']=counters.get('reply',0)+1 if received>0 else 0
    ready=metrics['warm'] and metrics['coverage']>=config['minimum_coverage']
    if counters['no_reply']>=config['unreachable_rounds']:
        conditions[unreachable]='critical'
    if unreachable in conditions and counters['reply']>=config['recovery_rounds']:
        del conditions[unreachable]
        conditions['recovery_pending']='warning'
    overall_good=ready
    for key,value,warning,critical,recovery in [
        ('loss' if config.get('protocol','icmp')=='icmp' else 'availability',metrics['loss_percent'] if config.get('protocol','icmp')=='icmp' else metrics['failure_percent'],config['loss_warning'],config['loss_critical'],config['loss_recovery']),
        ('latency',metrics['p95_ms'],config['rtt_warning'],config['rtt_critical'],
         config['rtt_warning']*config['rtt_recovery_ratio'] if config['rtt_warning'] is not None else None),
    ]:
        if warning is None:
            if key in conditions:overall_good=False
            continue
        if not ready or value is None:
            counters[key]={};overall_good=False;continue
        tally=counters.get(key,{})
        candidate='critical' if value>=critical else 'warning' if value>=warning else None
        tally['bad']=tally.get('bad',0)+1 if candidate and tally.get('candidate')==candidate else (1 if candidate else 0)
        tally['candidate']=candidate
        tally['good']=tally.get('good',0)+1 if value<recovery else 0
        tally['lower']=tally.get('lower',0)+1 if value<critical*config['rtt_recovery_ratio'] else 0
        if candidate and tally['bad']>=config['persistence']:
            if conditions.get(key)!='critical' or candidate=='critical':conditions[key]=candidate
        if conditions.get(key)=='critical' and tally['lower']>=config['recovery_rounds']:
            conditions[key]='warning'
        if tally['good']>=config['recovery_rounds']:
            conditions.pop(key,None)
        if value>=recovery:overall_good=False
        counters[key]=tally
    counters['recovery_good']=counters.get('recovery_good',0)+1 if overall_good and received>0 else 0
    if counters['recovery_good']>=config['recovery_rounds']:
        conditions.pop('recovery_pending',None)
    return conditions,counters


def record_configuration(db,target,kind,stamp):
    for incident in db.scalars(select(ProbeIncident).where(ProbeIncident.target_id==target.id,ProbeIncident.closed_at.is_(None))):
        db.add(ProbeIncidentEvent(incident_id=incident.id,kind=kind,created_at=stamp,
            data={'target_version':target.version,'effective':target.effective}))


def evaluate(db,target,series,round_row,maintenance=False):
    # Caller owns the target row lock; incident/state/round are committed together.
    state=db.scalar(select(ProbeState).where(ProbeState.target_id==target.id,ProbeState.address==series.address))
    if not state:
        state=ProbeState(target_id=target.id,address=series.address,conditions={},counters={},metrics={})
        db.add(state);db.flush()
    if state.series_id==series.id and state.last_slot is not None and state.last_slot>=round_row.slot:return
    config=series.config;slot=round_row.slot;interval=config['interval_seconds']
    gap=state.series_id!=series.id or state.last_slot is None or state.last_slot+interval!=slot
    previous_warm=state.counters.get('warm_since',series.started_at) if state.series_id==series.id else series.started_at
    counters={} if gap else dict(state.counters)
    warm_since=counters.setdefault('warm_since',previous_warm)
    before=dict(state.conditions)
    rows=list(db.scalars(select(ProbeRound).where(ProbeRound.series_id==series.id,
        ProbeRound.slot>slot-config['window_seconds'],ProbeRound.slot<=slot).order_by(ProbeRound.slot)))
    metrics=window_metrics(rows,config,slot,warm_since)
    if maintenance or round_row.status!='valid':
        counters={'warm_since':slot+interval if maintenance else warm_since}
        conditions=before
        metrics['state']='maintenance' if maintenance else 'no_data'
    else:
        conditions,counters=transition(before,counters,metrics,config,round_row.received)
        metrics['state']='critical' if 'critical' in conditions.values() else 'warning' if conditions else (
            'healthy' if metrics['warm'] and metrics['coverage']>=config['minimum_coverage'] else 'warming')
    metrics['last_valid']=round_row.status=='valid'
    metrics['error_code']=round_row.error_code
    metrics['last_check']=round_row.details or {}
    state.series_id=series.id;state.last_slot=slot;state.conditions=conditions;state.counters=counters;state.metrics=metrics
    if maintenance or round_row.status!='valid':return
    incident=db.scalar(select(ProbeIncident).where(ProbeIncident.target_id==target.id,
        ProbeIncident.address==series.address,ProbeIncident.closed_at.is_(None)))
    severity='critical' if 'critical' in conditions.values() else 'warning'
    event=None
    if conditions and not incident:
        incident=ProbeIncident(target_id=target.id,address=series.address,pop_id=target.pop_id,
            opened_at=slot,severity=severity,conditions=conditions,profile=config)
        db.add(incident);db.flush();event='opened'
    elif incident and not conditions:
        incident.closed_at=slot;incident.conditions={};event='recovered'
    elif incident and (incident.conditions!=conditions or incident.severity!=severity):
        incident.conditions=conditions;incident.severity=severity;event='conditions_changed'
    if event:
        db.add(ProbeIncidentEvent(incident_id=incident.id,kind=event,created_at=slot,data={
            'conditions':conditions,'severity':severity,'metrics':metrics,'profile':config,
            'series_id':series.id,'samples':round_row.samples}))
    if event in ('opened','recovered'):
        # The path during the fault and after it, to compare with the baseline.
        trace.request(db,target,'incident_'+event,slot,incident_id=incident.id,preferred=series.address)
