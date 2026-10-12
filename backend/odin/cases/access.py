"""Access cases: what fails between an OLT and its customers, one case per OLT, for level 1 to report.

A case gathers the OLT's PON outages (SmartOLT), its uplinks down and whether SmartOLT reaches it, opens with the first
of them and recovers when none is left. Its entity is the OLT's inventory device when known, so cases of the same
equipment or POP and the router interfaces that reach the OLT show as related (cases/api.py).
ODIN's reading, deterministic and never a confirmed cause, in this order:
- SmartOLT does not reach the OLT: the OLT itself, its power or its uplink;
- an uplink is down: the likely cause of what its PONs show;
- fiber loss (LOS) on several PONs starting together: a cut common to them (feeder) or the OLT card;
- LOS on one PON: a cut in that PON's distribution, its zone and splitters;
- power: the customers' ONUs lost power (dying gasp): an electrical outage in the zone, not the network;
- offline: ONUs offline without a cause SmartOLT can tell.
Critical when the OLT is unreachable, an uplink is down, LOS hits several PONs together or AFFECTED_CRITICAL customers.
Outages SmartOLT lists as down over 7 days (stale) are known and stay out.
"""
from sqlalchemy import select
from ..models import Case, CaseEvidence, Olt, OltOutage, OltUplink, Pop

ACCESS = 'access'
AFFECTED_CRITICAL = 64
TOGETHER = 600          # PON outages starting this close share a cause
RULE_VERSION = 4
CAUSES = {'los': 'corte de fibra (LOS)', 'power': 'falla de energía', 'offline': 'sin conexión'}


def key(olt_id):
    # By SmartOLT's OLT ID: the inventory match may come later and must not split the case.
    return f'{ACCESS}|olt:{olt_id}'


def zones(pons):
    return ', '.join(sorted({p['zone'] for p in pons if p['zone']})) or 'sin zona'


def pon_text(p):
    return f'PON {p["board"]}/{p["port"]}'


def reading(name, unreachable, uplinks, pons):
    """(code, title, action, critical) for level 1; the code names the situation of its guide (noc/guides.py)."""
    los = [p for p in pons if p['cause'] == 'los']
    starts = sorted(p['started_at'] for p in los if p['started_at'])
    together = len(los) >= 2 and (not starts or starts[-1] - starts[0] <= TOGETHER)
    affected = sum(p['affected'] for p in pons)
    if unreachable:
        total = ' y tiene PON caídos: posible caída total de la OLT' if pons else ''
        return ('unreachable', f'SmartOLT no llega a la OLT {name}{total}',
                f'Informar caída de la OLT {name} ({zones(pons)}). N2 revisa energía y uplink del POP.', True)
    if uplinks:
        return ('uplink', f'Uplink {", ".join(uplinks)} de la OLT {name} caído' + (f': {len(pons)} PON con caídas' if pons else ''),
                f'Informar: la OLT {name} perdió su uplink ({zones(pons)}). N2 revisa el enlace hacia el POP.', True)
    if together:
        return ('fiber_common', f'Corte de fibra común: {len(los)} PON con LOS a la vez, {sum(p["affected"] for p in los)} ONUs ({zones(los)})',
                f'Informar corte de fibra en {zones(los)}. N2 despacha a campo: tramo común a esos PON o tarjeta de la OLT.', True)
    if los:
        p = max(los, key=lambda x: x['affected'])
        splitters = f', splitters {", ".join(p["odbs"][:5])}' if p['odbs'] else ''
        return ('fiber', f'Corte de fibra en el {pon_text(p)}: {p["affected"]} de {p["subscribers"]} ONUs (zona {p["zone"] or "sin zona"}{splitters})'
                + (f'; {len(los) - 1} PON más con LOS en otros momentos' if len(los) > 1 else ''),
                f'Informar corte de fibra en {zones(los)}. N2 despacha a campo.', affected >= AFFECTED_CRITICAL)
    power = [p for p in pons if p['cause'] == 'power']
    if power:
        return ('power', f'Falla de energía en la zona: {sum(p["affected"] for p in power)} ONUs sin energía ({zones(power)})',
                f'Informar falla eléctrica en {zones(power)}: no es la red. No requiere despacho.', affected >= AFFECTED_CRITICAL)
    return ('offline', f'ONUs sin conexión sin causa identificada: {affected} ONUs ({zones(pons)})',
            f'Informar y vigilar {zones(pons)}. Si sigue, N2 revisa el PON.', affected >= AFFECTED_CRITICAL)


def evidence(case, ref, at, title, reason, data=None):
    return CaseEvidence(case_id=case.id, source='smartolt', ref=str(ref)[:300], role='trigger', strength='entity', reason=reason,
                        occurred_at=at, title=title[:500], data=data or {}, rule_version=RULE_VERSION)


def ingest(db, touched, stamp):
    olts = list(db.scalars(select(Olt).where(Olt.present.is_(True))))
    if not olts:return
    outages = {}
    for o in db.scalars(select(OltOutage).where(OltOutage.cause != 'stale', OltOutage.active.is_(True))):outages.setdefault(o.olt_id, []).append(o)
    uplinks = {}
    for u in db.scalars(select(OltUplink).where(OltUplink.admin_status == 'Enabled')):
        if u.status.lower() == 'down':uplinks.setdefault(u.olt_id, []).append(u.name)
    pops = {p.id: p.name for p in db.scalars(select(Pop))}
    for olt in olts:
        current, down_links, unreachable = outages.get(olt.id, []), sorted(uplinks.get(olt.id, [])), not olt.reachable
        problem = bool(current or down_links or unreachable)
        case = db.scalar(select(Case).where(Case.key == key(olt.id), Case.state != 'closed'))
        if not case and not problem:continue
        if not case:
            start = min([o.started_at or o.first_seen for o in current] + [stamp])
            case = Case(kind=ACCESS, key=key(olt.id), entity_key='device:' + olt.device_id if olt.device_id else 'olt:' + olt.id,
                        entity_label=pops.get(olt.pop_id, 'OLT'), subject=olt.name, detail={}, opened_at=start, last_activity_at=stamp,
                        last_state='down', last_state_at=start, state='active', rule_version=RULE_VERSION)
            db.add(case);db.flush()
        # The inventory match and the POP can arrive after the case opened.
        case.entity_key = 'device:' + olt.device_id if olt.device_id else 'olt:' + olt.id
        case.entity_label = pops.get(olt.pop_id, 'OLT')
        before = dict(case.detail);changed = False
        linked = set(db.scalars(select(CaseEvidence.ref).where(CaseEvidence.case_id == case.id, CaseEvidence.source == 'smartolt')))
        # Every outage of this OLT in the case's life, ended ones included, so recoveries are recorded.
        for o in db.scalars(select(OltOutage).where(OltOutage.olt_id == olt.id, OltOutage.cause != 'stale', OltOutage.first_seen >= case.opened_at - TOGETHER)):
            what = f'PON {o.board}/{o.port}: {CAUSES.get(o.cause, o.cause)} · {o.affected} de {o.subscribers} ONUs' + (f' · zona {o.zone}' if o.zone else '')
            if o.id not in linked:
                db.add(evidence(case, o.id, o.started_at or o.first_seen, what, 'Caída de PON de la misma OLT (SmartOLT)',
                                {'cause': o.cause, 'affected': o.affected, 'odbs': o.odbs}));changed = True
            if not o.active and o.id + ':ended' not in linked:
                db.add(evidence(case, o.id + ':ended', o.ended_at, f'PON {o.board}/{o.port}: restablecido', 'Fin de la caída de PON (SmartOLT)'));changed = True
        if unreachable != bool(before.get('unreachable')):
            db.add(evidence(case, f'{olt.id}:reach:{stamp}', stamp, 'SmartOLT no llega a la OLT' if unreachable else 'SmartOLT vuelve a llegar a la OLT',
                            'Alcance de la OLT desde SmartOLT'));changed = True
        for name in sorted(set(down_links) ^ set(before.get('uplinks_down') or [])):
            db.add(evidence(case, f'{olt.id}:uplink:{name}:{stamp}', stamp, f'Uplink {name} ' + ('caído' if name in down_links else 'restablecido'),
                            'Uplink de la misma OLT (SmartOLT)'));changed = True
        pons = [{'board': o.board, 'port': o.port, 'cause': o.cause, 'zone': o.zone, 'affected': o.affected, 'subscribers': o.subscribers,
                 'started_at': o.started_at or o.first_seen, 'odbs': o.odbs[:10]} for o in sorted(current, key=lambda x: (x.board, x.port))]
        # Only its own fields: impact and evidence totals belong to other passes of the correlator.
        detail = {**before, 'scope': 'access', 'olt_id': olt.id, 'device_id': olt.device_id, 'pop_id': olt.pop_id, 'unreachable': unreachable,
                  'uplinks_down': down_links, 'pons': pons, 'affected': sum(p['affected'] for p in pons),
                  'subscribers': sum(p['subscribers'] for p in pons), 'zones': sorted({p['zone'] for p in pons if p['zone']})}
        if problem:
            code, title, action, critical = reading(olt.name, unreachable, down_links, pons)
            detail['reading'] = {'code': code, 'title': title, 'action': action}
            case.severity = 'critical' if critical else 'warning'
            if case.last_state != 'down':case.last_state, case.last_state_at, case.resolved_at = 'down', stamp, None;changed = True
        else:
            detail['reading'] = before.get('reading')
            if case.last_state != 'up':case.last_state, case.last_state_at, case.resolved_at = 'up', stamp, stamp;changed = True
        if detail != before:case.detail = detail
        if changed:
            case.last_activity_at = stamp
            refs = set(db.scalars(select(CaseEvidence.ref).where(CaseEvidence.case_id == case.id, CaseEvidence.source == 'smartolt')))
            case.drops = sum(1 for r in refs if ':' not in r)  # one per PON outage; reach and uplink changes carry the OLT ID
            case.recoveries = sum(1 for r in refs if r.endswith(':ended'))
            touched.add(case.id)
