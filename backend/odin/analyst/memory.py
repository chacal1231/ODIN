"""Memory for the analyst: earlier cases of the same subject and knowledge validated by the NOC.

Earlier cases (M) carry their last analysis summary, the NOC's verdict and correction, and their
notes: a background, never proof of the current case. Knowledge entries (K) are written by
administrators and matched by case kind and keywords; the model's text never becomes knowledge
by itself. Private conversations are never searched.
"""
from sqlalchemy import select, or_, true
from ..models import Analysis, AnalysisReview, Case, CaseNote, Knowledge
from .packs import Line, KINDS, stamp_text

SAME_SUBJECT = 3
SAME_DEVICE = 2
KNOWLEDGE = 3
VERDICTS = {'correct': 'correcto', 'partial': 'parcialmente correcto', 'incorrect': 'incorrecto'}


def case_memory(db, case):
    earlier = list(db.scalars(select(Case).where(Case.key == case.key, Case.id != case.id, Case.opened_at < case.opened_at)
                              .order_by(Case.opened_at.desc()).limit(SAME_SUBJECT)))
    # Same device, other subjects: only cases the NOC already looked at (analysed, reviewed or annotated).
    seen = select(Analysis.case_id).join(AnalysisReview, AnalysisReview.analysis_id == Analysis.id)
    noted = select(CaseNote.case_id)
    earlier += list(db.scalars(select(Case).where(Case.entity_key == case.entity_key, Case.key != case.key, Case.opened_at < case.opened_at,
                                                  or_(Case.id.in_(seen), Case.id.in_(noted))).order_by(Case.opened_at.desc()).limit(SAME_DEVICE)))
    lines = []
    for index, other in enumerate(earlier, 1):
        same = other.key == case.key
        text = (f'M{index} · caso anterior {"de la misma " + KINDS.get(other.kind, other.kind) if same else "del mismo equipo"}: '
                f'{KINDS.get(other.kind, other.kind)} {other.subject} en {other.entity_label}, {other.drops} caídas, '
                f'desde {stamp_text(other.opened_at)} hasta {stamp_text(other.closed_at or other.last_activity_at)}')
        analysis = db.scalar(select(Analysis).where(Analysis.case_id == other.id, Analysis.status == 'done').order_by(Analysis.finished_at.desc()).limit(1))
        if analysis:
            text += f'; análisis de entonces: «{analysis.result["resumen"][:300]}»'
            for review in db.scalars(select(AnalysisReview).where(AnalysisReview.analysis_id == analysis.id).order_by(AnalysisReview.created_at)):
                text += f'; valoración del NOC: {VERDICTS[review.verdict]}' + (f', corrección: «{review.correction[:400]}»' if review.correction else '')
        for note in db.scalars(select(CaseNote).where(CaseNote.case_id == other.id).order_by(CaseNote.created_at).limit(2)):
            text += f'; nota del NOC: «{note.text[:300]}»'
        lines.append(Line(f'M{index}', other.opened_at, 2, text, {'kind': 'case', 'ids': [other.id], 'count': 1}))
    return lines


def knowledge_lines(db, kind, text):
    """Knowledge for this kind of case (or any) whose keywords appear in the context or the question."""
    haystack = text.casefold()
    rows = db.scalars(select(Knowledge).where(Knowledge.archived.is_(False), or_(Knowledge.case_kind.is_(None), Knowledge.case_kind == kind) if kind else true())
                      .order_by(Knowledge.updated_at.desc()))
    found = []
    for row in rows:
        keywords = [k for k in row.keywords if k.strip()]
        if keywords and not any(k.casefold() in haystack for k in keywords):continue
        found.append(row)
        if len(found) == KNOWLEDGE:break
    return [Line(f'K{index}', row.updated_at, 1, f'K{index} · conocimiento validado por el NOC «{row.title}»: «{row.body[:900]}»',
                 {'kind': 'knowledge', 'ids': [row.id], 'count': 1}) for index, row in enumerate(found, 1)]
