import { FormEvent, useEffect, useState } from "react";

type Api = (path: string, method?: string, body?: unknown) => Promise<any>;
type Ref = { kind: string; source?: string; ids: string[]; count: number };

const stages: Record<string, string> = {
  queued: "En cola", gathering: "Reuniendo evidencias", reading: "Leyendo evidencias", writing: "Redactando",
  validating: "Validando referencias", delayed: "Demorado: Qwen no responde, se reintentará",
};
const errors: Record<string, string> = {
  analyst_not_configured: "El analista no está activado", pack_too_large: "La evidencia no cabe ni agrupada",
  invalid_output: "Qwen no devolvió una respuesta válida", unknown_references: "Qwen citó evidencias que no existen",
  output_truncated: "La respuesta superó el límite de longitud", connection_failed: "No se pudo conectar con Qwen",
  llm_timeout: "Qwen no respondió a tiempo", server_unavailable: "Qwen no está disponible", access_denied: "Qwen rechazó el token",
  lease_expired: "El análisis perdió su turno en la cola", cancelled: "Cancelado",
};
const confidence: Record<string, string> = { baja: "confianza baja", media: "confianza media", alta: "confianza alta" };
const verdicts: [string, string][] = [["correct", "Correcto"], ["partial", "Parcialmente correcto"], ["incorrect", "Incorrecto"]];
const verdictNames = Object.fromEntries(verdicts);

export function CaseAnalysis({ api, zone, caseId, onFocus }: {
  api: Api; zone: string; caseId: string; onFocus: (label: string, ref: Ref) => void;
}) {
  const [data, setData] = useState<any>(null);
  const [error, setError] = useState("");
  const [shown, setShown] = useState(0);
  const [refresh, setRefresh] = useState(0);
  const date = (stamp: number | null) => stamp == null ? "—" : new Date(stamp * 1000).toLocaleString("es", { timeZone: zone, dateStyle: "short", timeStyle: "short" });
  const busy = !!data?.items.some((a: any) => a.status === "pending" || a.status === "running");
  useEffect(() => {
    let active = true;
    const load = () => api(`/cases/${caseId}/analyses`).then((d) => { if (active) { setData(d); setError(""); } })
      .catch((e) => { if (active) setError((e as Error).message); });
    void load();
    const timer = busy ? window.setInterval(load, 3000) : undefined;
    return () => { active = false; if (timer) window.clearInterval(timer); };
  }, [api, caseId, refresh, busy]);
  useEffect(() => setShown(0), [caseId]);
  const act = async (fn: () => Promise<unknown>) => {
    try { await fn(); setShown(0); setRefresh((n) => n + 1); } catch (e) { setError((e as Error).message); }
  };
  if (!data) return null;
  const analysis = data.items[shown];
  const latest = data.items[0];
  const running = latest && (latest.status === "pending" || latest.status === "running") ? latest : null;
  return <div className="probe-profile analysis">
    <div className="title-row"><strong>Análisis de Qwen</strong>
      {data.available && !running && <button className="quiet" onClick={() => void act(() => api(`/cases/${caseId}/analyses`, "POST"))}>
        {latest ? "Volver a analizar" : "Analizar"}</button>}
    </div>
    {!data.available && !latest && <p className="muted">El analista no está activado. Un administrador lo activa en Casos cuando Qwen esté configurado en Administración.</p>}
    {data.available && !latest && <p className="muted">Qwen lee las evidencias agrupadas de este caso y separa hechos con referencias, hipótesis, datos faltantes y comprobaciones sugeridas. Tarda uno o dos minutos y no ejecuta nada sobre la red.</p>}
    {running && <p role="status">
      {stages[running.stage] || running.stage}
      {running.queue_position > 1 && ` · posición ${running.queue_position} en la cola`}
      {running.stage === "reading" && running.progress?.total ? ` · ${Math.round(100 * running.progress.done / running.progress.total)} %` : ""}
      {running.error && running.stage === "delayed" ? ` (${errors[running.error] || running.error})` : ""}
      {" "}<button className="quiet" onClick={() => void act(() => api(`/analyses/${running.id}/cancel`, "POST"))}>Cancelar</button>
    </p>}
    {analysis && analysis !== running && <AnalysisResult api={api} analysis={analysis} outdated={analysis.input_version && analysis.input_version !== data.version}
      date={date} onFocus={onFocus} onReview={() => setRefresh((n) => n + 1)} />}
    {data.items.length > 1 && <p className="muted">Análisis anteriores:{" "}
      {data.items.map((a: any, i: number) => a.status === "done" && <button key={a.id} className="link" aria-pressed={i === shown} onClick={() => setShown(i)}>
        {date(a.finished_at)}</button>)}</p>}
    {error && <p role="alert">{error}</p>}
  </div>;
}

function AnalysisResult({ api, analysis, outdated, date, onFocus, onReview }: {
  api: Api; analysis: any; outdated: boolean; date: (s: number | null) => string; onFocus: (label: string, ref: Ref) => void; onReview: () => void;
}) {
  if (analysis.status !== "done") return <p className="muted">
    {analysis.status === "cancelled" ? "Último análisis cancelado." : `Último análisis fallido: ${errors[analysis.error] || analysis.error || "error"}.`}
    {" "}{date(analysis.finished_at)}</p>;
  const r = analysis.result;
  const chips = (refs: string[]) => refs.map((label) => {
    const ref = analysis.refs[label];
    return <button key={label} className="chip ref-chip" title={ref ? `${ref.count} ${ref.count === 1 ? "elemento" : "elementos"}` : ""}
      onClick={() => ref && onFocus(label, ref)}>{label}</button>;
  });
  return <>
    {outdated && <p className="notice">El caso cambió después de este análisis (nueva actividad, evidencias o notas). Vuelve a analizar para incluirlo.</p>}
    <p>{r.resumen}</p>
    {!!r.hechos.length && <><h4>Hechos</h4><ul className="case-impact">{r.hechos.map((h: any, i: number) => <li key={i}>{h.afirmacion} {chips(h.evidencias)}</li>)}</ul></>}
    {!!r.hipotesis.length && <><h4>Hipótesis</h4><ul className="case-impact">{r.hipotesis.map((h: any, i: number) => <li key={i}>
      {h.hipotesis} <span className="badge">{confidence[h.confianza]}</span>
      {!!h.a_favor.length && <> · a favor {chips(h.a_favor)}</>}{!!h.en_contra.length && <> · en contra {chips(h.en_contra)}</>}</li>)}</ul></>}
    {!!r.datos_faltantes.length && <><h4>Datos faltantes</h4><ul className="case-impact">{r.datos_faltantes.map((d: string, i: number) => <li key={i}>{d}</li>)}</ul></>}
    {!!r.recomendaciones.length && <><h4>Comprobaciones sugeridas</h4><p className="muted">Para revisión del NOC; ODIN no ejecuta nada.</p>
      <ul className="case-impact">{r.recomendaciones.map((d: any, i: number) => <li key={i}>{d.accion} <small>· Cómo comprobarlo: {d.comprobacion}</small> {chips(d.evidencias)}</li>)}</ul></>}
    <p className="muted">
      {date(analysis.finished_at)} · {analysis.model?.split("/").pop()} · {analysis.pack_tokens} tokens de evidencia · {Math.round((analysis.duration_ms || 0) / 1000)} s
      {analysis.requested_by && ` · pedido por ${analysis.requested_by}`}
      {!!analysis.omitted.length && <><br />No se incluyeron por espacio: {analysis.omitted.join("; ")}.</>}
    </p>
    <details><summary>Cobertura de fuentes que leyó</summary><ul className="case-impact">{analysis.coverage.map((c: string) => <li key={c}>{c}</li>)}</ul></details>
    <Review api={api} analysis={analysis} date={date} onDone={onReview} />
  </>;
}

function Review({ api, analysis, date, onDone }: { api: Api; analysis: any; date: (s: number | null) => string; onDone: () => void }) {
  const [verdict, setVerdict] = useState("");
  const [correction, setCorrection] = useState("");
  const [error, setError] = useState("");
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    try { await api(`/analyses/${analysis.id}/reviews`, "POST", { verdict, correction }); setVerdict(""); setCorrection(""); onDone(); }
    catch (err) { setError((err as Error).message); }
  };
  return <>
    {analysis.reviews.map((r: any, i: number) => <p key={i}><small>{date(r.created_at)} · {r.author || "usuario"} · {verdictNames[r.verdict]}</small>
      {r.correction && <><br />{r.correction}</>}</p>)}
    <form className="event-filters" onSubmit={submit}>
      <label>Valoración del NOC<select value={verdict} onChange={(e) => setVerdict(e.target.value)} required>
        <option value="">Elegir…</option>{verdicts.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
      </select></label>
      {verdict && verdict !== "correct" && <label>Qué es incorrecto y qué ocurrió en realidad
        <input value={correction} maxLength={3000} onChange={(e) => setCorrection(e.target.value)} required /></label>}
      <button disabled={!verdict}>Registrar</button>
    </form>
    <small className="muted">Las correcciones se usan como memoria en los análisis de casos parecidos.</small>
    {error && <p role="alert">{error}</p>}
  </>;
}
