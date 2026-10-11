import { FormEvent, useEffect, useRef, useState } from "react";
import { Drawer } from "./drawer";
import { Paged } from "./pager";
import { ProbeBadge } from "./probes";
import { CaseAnalysis } from "./analysis";
import type { ChatCase } from "./chat";
import { KnowledgePanel, KnowledgeDraft } from "./knowledge";

type Api = (path: string, method?: string, body?: unknown) => Promise<any>;

const kinds: Record<string, string> = { bgp_session: "Sesión BGP", interface: "Interfaz", latency: "Latencia hacia" };
// What the neighbor or the interface is, as the correlator read it from the router port descriptions.
const role = (c: any) => {
  const n = c.detail?.neighbor || c.detail?.port;
  return n?.remote_device_name ? `Enlace interno con ${n.remote_device_name}` : n?.label ? n.label[0].toUpperCase() + n.label.slice(1) : "";
};
// A latency case is degraded or normal; sessions and interfaces go down and come back.
const down = (c: any) => c.kind === "latency" ? (c.last_state === "down" ? "degradado" : "normal") : (c.last_state === "down" ? "caída" : "restablecida");
const states: Record<string, string> = { active: "Activo", stale: "Sin actividad", closed: "Cerrado" };
const filters: [string, string][] = [["alerting", "Avisan en la barra"], ["open", "Abiertos"], ["active", "Activos"], ["stale", "Sin actividad"], ["closed", "Cerrados"], ["all", "Todos"]];
const sources: Record<string, string> = { graylog: "Logs", zabbix: "Zabbix", librenms: "LibreNMS", probe: "Latencia" };
const roles: Record<string, string> = { trigger: "Cambio de estado", context: "Contexto", impact: "Impacto" };
const strengths: Record<string, string> = { entity: "Mismo vecino o interfaz", device: "Mismo equipo", time: "Solo coincidencia temporal" };

function duration(seconds: number) {
  if (seconds < 3600) return `${Math.max(1, Math.round(seconds / 60))} min`;
  if (seconds < 172800) return `${(seconds / 3600).toFixed(1).replace(".", ",")} h`;
  return `${Math.round(seconds / 86400)} días`;
}

const providerNames: Record<string, string> = { llama_server: "modelo local", deepseek: "DeepSeek" };
export function Cases({ api, zone, admin, focusId, onAsk }: { api: Api; zone: string; admin: boolean; focusId?: string; onAsk?: (c: ChatCase) => void }) {
  const [filter, setFilter] = useState("open");
  const [kind, setKind] = useState("");
  const [data, setData] = useState<any>(null);
  const [more, setMore] = useState<any[]>([]);
  const [selected, setSelected] = useState<string | null>(focusId || null);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const chosen = useRef(false);
  const date = (stamp: number | null) => stamp == null ? "—" : new Date(stamp * 1000).toLocaleString("es", { timeZone: zone, dateStyle: "short", timeStyle: "short" });
  useEffect(() => { if (focusId) { setSelected(focusId); setFilter("all"); } }, [focusId]);
  useEffect(() => {
    let active = true;
    const load = async () => {
      const query = new URLSearchParams({ state: filter });
      if (kind) query.set("kind", kind);
      try {
        const d = await api("/cases?" + query);
        if (!active) return;
        // With alerts on, the list opens on what the status bar counts.
        if (!chosen.current && !focusId && d.correlator?.alerts_enabled && filter === "open") { chosen.current = true; setFilter("alerting"); return; }
        chosen.current = true; setData(d); setMore([]); setError("");
      }
      catch (e) { if (active) setError((e as Error).message); }
    };
    void load();
    const timer = window.setInterval(load, 60000);
    return () => { active = false; window.clearInterval(timer); };
  }, [api, filter, kind, refresh]);
  const loadMore = async () => {
    const query = new URLSearchParams({ state: filter, before: data.next });
    if (kind) query.set("kind", kind);
    try { const d = await api("/cases?" + query); setMore([...more, ...d.items]); setData({ ...data, next: d.next }); }
    catch (e) { setError((e as Error).message); }
  };
  const toggleAlerts = async () => {
    try { await api("/cases/settings/alerts", "PUT", { alerts_enabled: !data.correlator.alerts_enabled }); setRefresh((n) => n + 1); }
    catch (e) { setError((e as Error).message); }
  };
  const [analyst, setAnalyst] = useState<any>(null);
  const [draft, setDraft] = useState<KnowledgeDraft | null>(null);
  const [knowledgeFocus, setKnowledgeFocus] = useState<string | null>(null);
  useEffect(() => { api("/analyst/status").then(setAnalyst).catch(() => setAnalyst(null)); }, [api, refresh]);
  // One provider at a time: turning one on turns the other off.
  const setAnalystProvider = async (provider: string, enabled: boolean) => {
    const row = analyst.providers.find((p: any) => p.kind === provider);
    try { await api("/analyst/settings", "PUT", { provider, version: row.version, enabled }); setRefresh((n) => n + 1); }
    catch (e) { setError((e as Error).message); }
  };
  const items = [...(data?.items || []), ...more];
  const correlator = data?.correlator;
  return <>
    <section className="panel probe-panel">
      <div className="title-row"><h2>Casos</h2>
        {admin && correlator && <button className="quiet" onClick={() => void toggleAlerts()}>
          {correlator.alerts_enabled ? "Dejar de avisar en la barra de estado" : "Avisar en la barra de estado"}</button>}
      </div>
      <p>ODIN agrupa los cambios de estado de cada sesión BGP e interfaz en casos y les asocia evidencias de las demás fuentes, explicando por qué se relacionan. Una relación no demuestra la causa.</p>
      {correlator && !correlator.alerts_enabled && <p className="notice">Modo sombra: los casos se calculan y se pueden revisar aquí, pero todavía no avisan en la barra de estado.</p>}
      <Calibration api={api} />
      {error && <p role="alert">No se pudo actualizar: {error}</p>}
      {correlator && <p className="muted">Última pasada del correlador: {date(correlator.last_success_at)}{correlator.last_error && ` · Error: ${correlator.last_error}`} · Reglas versión {data.rule_version}</p>}
      {analyst?.configured && <p className="muted">Analista ({providerNames[analyst.provider] || analyst.provider}{analyst.model ? ` · ${analyst.model}` : ""}): {analyst.enabled ? "activo" : "desactivado"}
        {analyst.enabled && analyst.last_success_at && ` · última respuesta ${date(analyst.last_success_at)}`}
        {analyst.enabled && analyst.queued > 0 && ` · ${analyst.queued} en cola`}
        {analyst.enabled && analyst.last_error && (analyst.last_failure_at || 0) > (analyst.last_success_at || 0) && ` · último error: ${analyst.last_error}`}
        {admin && <> <button className="quiet" onClick={() => void setAnalystProvider(analyst.provider, !analyst.enabled)}>{analyst.enabled ? "Desactivar" : "Activar el analista"}</button>
          {analyst.providers.filter((p: any) => p.configured && p.kind !== analyst.provider).map((p: any) =>
            <button key={p.kind} className="quiet" onClick={() => void setAnalystProvider(p.kind, true)}>Usar {providerNames[p.kind]}</button>)}</>}</p>}
      <div className="event-filters">
        <label>Estado<select value={filter} onChange={(e) => { setFilter(e.target.value); setSelected(null); }}>
          {filters.map(([key, label]) => <option key={key} value={key}>{label}{data?.counts && (key in states || key === "alerting") ? ` (${data.counts[key] || 0})` : ""}</option>)}
        </select></label>
        <label>Tipo<select value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="">Todos</option>{Object.entries(kinds).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
        </select></label>
      </div>
      {data && !items.length && <p>No hay casos con estos filtros.</p>}
      {!!items.length && <div className="probe-table"><table className="noc-table">
        <thead><tr><th>Severidad</th><th>Caso</th><th>Equipo</th><th>Situación</th><th>Caídas / incidentes</th><th>Inicio</th><th>Última actividad</th></tr></thead>
        <tbody>{items.map((c) => <tr key={c.id} className={selected === c.id ? "selected" : ""}>
          <td><ProbeBadge state={c.severity} /></td>
          <td><button className="link" aria-pressed={selected === c.id} onClick={() => setSelected(c.id)}>{kinds[c.kind]} {c.subject}</button>
            {role(c) && <small>{role(c)}</small>}</td>
          <td>{c.entity_label}{c.entity_key.startsWith("name:") && <small> · sin vincular</small>}</td>
          <td>{states[c.state]} · {down(c)}{c.acknowledged_at ? " · reconocido" : ""}</td>
          <td>{c.drops}</td><td className="time">{date(c.opened_at)}</td><td className="time">{date(c.last_activity_at)}</td>
        </tr>)}</tbody>
      </table></div>}
      {data?.next && <button className="quiet" onClick={() => void loadMore()}>Cargar más</button>}
    </section>
    {selected && <Drawer label="Caso" link={"#caso=" + selected} onClose={() => setSelected(null)}>
      <CaseDetail api={api} zone={zone} admin={admin} id={selected} onSelect={setSelected} onChange={() => setRefresh((n) => n + 1)} onAsk={onAsk}
      onDraft={setDraft} onKnowledge={(k) => { setSelected(null); setKnowledgeFocus(null); window.setTimeout(() => setKnowledgeFocus(k)); }} /></Drawer>}
    <KnowledgePanel api={api} admin={admin} draft={draft} focusId={knowledgeFocus} onDone={() => setDraft(null)} />
  </>;
}

function CaseDetail({ api, zone, admin, id, onSelect, onChange, onAsk, onDraft, onKnowledge }: {
  api: Api; zone: string; admin: boolean; id: string; onSelect: (id: string) => void; onChange: () => void; onAsk?: (c: ChatCase) => void;
  onDraft: (d: KnowledgeDraft) => void; onKnowledge: (id: string) => void;
}) {
  const [data, setData] = useState<any>(null);
  const [error, setError] = useState("");
  const [showTriggers, setShowTriggers] = useState(false);
  const [note, setNote] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [highlight, setHighlight] = useState<Set<string>>(new Set());
  const date = (stamp: number | null) => stamp == null ? "—" : new Date(stamp * 1000).toLocaleString("es", { timeZone: zone });
  useEffect(() => setHighlight(new Set()), [id]);
  // A cited label marks its rows in the timeline (or opens the related case) and scrolls to the first one.
  const focus = (label: string, ref: { kind: string; ids: string[] }) => {
    if (ref.kind === "case") { onSelect(ref.ids[0]); return; }
    if (ref.kind === "knowledge") { onKnowledge(ref.ids[0]); return; }
    const ids = new Set(ref.ids);
    if (ref.kind === "evidence" && data.evidence.some((e: any) => e.role === "trigger" && ids.has(e.id))) setShowTriggers(true);
    setHighlight(ids);
    window.setTimeout(() => {
      const first = ref.ids.map((x) => document.getElementById("ev-" + x)).find(Boolean);
      (first || document.getElementById(ref.kind === "note" ? "case-notes" : "case-timeline"))?.scrollIntoView({ behavior: "smooth", block: "center" });
    }, 50);
  };
  useEffect(() => {
    let active = true;
    setData(null);
    api("/cases/" + id).then((d) => { if (active) { setData(d); setError(""); } }).catch((e) => { if (active) setError((e as Error).message); });
    return () => { active = false; };
  }, [api, id, refresh]);
  const act = async (fn: () => Promise<unknown>) => {
    try { await fn(); setRefresh((n) => n + 1); onChange(); } catch (e) { setError((e as Error).message); }
  };
  const addNote = (e: FormEvent) => { e.preventDefault(); void act(async () => { await api(`/cases/${id}/notes`, "POST", { text: note }); setNote(""); }); };
  if (error && !data) return <section className="panel probe-panel"><p role="alert">{error}</p></section>;
  if (!data) return <section className="panel probe-panel"><p className="muted">Consultando…</p></section>;
  const impact = data.evidence.filter((e: any) => e.role === "impact");
  const timeline = data.evidence.filter((e: any) => showTriggers || e.role !== "trigger");
  const end = data.closed_at || data.last_activity_at;
  return <section className="panel probe-panel">
    <div className="title-row"><h2>{kinds[data.kind]} {data.subject} · {data.entity_label}</h2>
      <span>{onAsk && <button className="quiet" onClick={() => onAsk({ id, label: `${kinds[data.kind]} ${data.subject} · ${data.entity_label}` })}>Preguntar en el chat</button>}
        {admin && <button className="quiet" onClick={() => onDraft({ title: `${kinds[data.kind]} ${data.subject} · ${data.entity_label}`, body: "", case_kind: data.kind,
          keywords: data.subject, source_case_id: id })}>Guardar como conocimiento</button>}
        {!data.acknowledged_at && <button className="quiet" onClick={() => void act(() => api(`/cases/${id}/acknowledge`, "POST"))}>Reconocer</button>}</span>
    </div>
    <p><ProbeBadge state={data.severity} /> {states[data.state]} · {down(data)} desde {date(data.last_state_at)}</p>
    {data.isolation && <p className="notice">{data.isolation}</p>}
    <dl className="event-detail">
      <dt>Período</dt><dd>{date(data.opened_at)} — {data.closed_at ? date(data.closed_at) : "en curso"} ({duration(end - data.opened_at)})</dd>
      <dt>Cambios de estado</dt><dd>{data.kind === "latency" ? `${data.drops} incidentes de latencia, ${data.recoveries} cerrados` : `${data.drops} caídas y ${data.recoveries} recuperaciones`}</dd>
      {data.kind === "bgp_session" && <><dt>Qué es el vecino</dt><dd>{data.neighbor
        ? <><span className="badge">{role(data) || "Sin clasificar"}</span> {data.neighbor.summary}</>
        : "Ninguna interfaz del inventario contiene su IP (multihop o equipo sin IP en LibreNMS)"}</dd></>}
      {data.kind === "interface" && data.port && <><dt>Qué es la interfaz</dt><dd><span className="badge">{role(data) || "Sin clasificar"}</span> «{data.port.description}»</dd></>}
      {data.detail?.vrf && <><dt>VRF</dt><dd>{data.detail.vrf}</dd></>}
      {data.detail?.scope === "external"
        ? <><dt>Servicio externo</dt><dd>Todas sus regiones. No comparte equipo con la red propia: los casos relacionados solo coinciden en el tiempo.</dd></>
        : <><dt>Identidad del equipo</dt><dd>{data.identity || "Solo el nombre de los logs"}{!data.entity_confirmed && " · sin vínculo confirmado con el inventario"}</dd></>}
      {data.acknowledged_at && <><dt>Reconocido</dt><dd>{date(data.acknowledged_at)}</dd></>}
    </dl>
    <CaseAnalysis api={api} zone={zone} caseId={id} onFocus={focus} />
    {!!impact.length && <><h3>Impacto</h3><ul className="case-impact">{impact.map((e: any) => <li key={e.id}><strong>{sources[e.source]}:</strong> {e.title} <small>· {e.reason}</small></li>)}</ul></>}
    {!!data.related.length && <><h3>Casos relacionados</h3><ul className="case-impact">{data.related.map((r: any) => <li key={r.id}>
      <button className="link" onClick={() => onSelect(r.id)}>{kinds[r.kind]} {r.subject} · {r.entity_label}</button> <small>· {r.relation} · {r.drops} {r.kind === "latency" ? "incidentes" : "caídas"}</small></li>)}</ul></>}
    <div className="title-row" id="case-timeline"><h3>Cronología</h3>
      <label className="checkbox-label"><input type="checkbox" checked={showTriggers} onChange={(e) => setShowTriggers(e.target.checked)} />
        Mostrar los cambios de estado ({data.triggers_total}{data.triggers_total > 300 ? ", se muestran los 300 más recientes" : ""})</label></div>
    <p className="muted">Ventana analizada: {date(data.window.start)} — {date(data.window.end)}. Cada línea indica de qué fuente viene y por qué se asoció.</p>
    {!timeline.length && <p>Sin evidencias asociadas además de los cambios de estado.</p>}
    {!!timeline.length && <Paged items={timeline} focus={(e: any) => highlight.has(e.id)}>{(page) => <div className="probe-table"><table className="noc-table">
      <thead><tr><th>Hora</th><th>Fuente</th><th>Tipo</th><th>Motivo</th><th>Detalle</th></tr></thead>
      <tbody>{page.map((e: any) => <tr key={e.id} id={"ev-" + e.id} className={highlight.has(e.id) ? "selected" : ""}>
        <td className="time">{date(e.occurred_at)}</td><td>{sources[e.source] || e.source}</td><td>{roles[e.role]}</td>
        <td>{e.reason}<small> · {strengths[e.strength]}</small></td><td>{e.title}</td>
      </tr>)}</tbody>
    </table></div>}</Paged>}
    <h3 id="case-notes">Notas</h3>
    {data.notes.map((n: any, i: number) => <p key={i} id={"ev-" + n.id} className={highlight.has(n.id) ? "highlight" : ""}><small>{date(n.created_at)} · {n.author || "usuario"}</small><br />{n.text}</p>)}
    <form className="event-filters" onSubmit={addNote}>
      <label>Nueva nota<input value={note} maxLength={2000} onChange={(e) => setNote(e.target.value)} required /></label>
      <button>Añadir</button>
    </form>
    {error && <p role="alert">{error}</p>}
  </section>;
}

// How many cases would alert per day under the current rules, before switching alerts on.
function Calibration({ api }: { api: Api }) {
  const [data, setData] = useState<any>(null);
  const [error, setError] = useState("");
  const load = () => api("/cases/calibration").then((d) => { setData(d); setError(""); }).catch((e) => setError((e as Error).message));
  const keys = data ? Object.keys(data.groups) : [];
  const average = data?.days.length ? Math.round(data.days.reduce((n: number, d: any) => n + d.alerts, 0) / data.days.length) : 0;
  const t = data?.thresholds;
  const reviewed = data ? ["correct", "partial", "incorrect"].reduce((n, k) => n + (data.reviews[k] || 0), 0) : 0;
  return <details onToggle={(e) => { if ((e.target as HTMLDetailsElement).open && !data) void load(); }}>
    <summary>Calibración de avisos</summary>
    {error && <p role="alert">{error}</p>}
    {data && <>
      <p>Con las reglas actuales, en los últimos 7 días habrían avisado <strong>{average} casos al día</strong> de media.
        Avisa: tránsito, transporte o enlace interno desde la primera caída; otras caídas solo si se sostienen {t.sustained_minutes} min o suman {t.flap_critical} caídas;
        latencia de red propia crítica; servicios externos solo cuando {t.external_together} o más se degradan a la vez.</p>
      <div className="probe-table"><table className="noc-table">
        <thead><tr><th>Día</th>{keys.map((k) => <th key={k} className="num">{data.groups[k]}</th>)}<th className="num">Avisan</th>
          <th className="num">No avisan (red)</th><th className="num">No avisan (externos)</th></tr></thead>
        <tbody>{data.days.map((d: any) => <tr key={d.day}><td>{d.day}</td>{keys.map((k) => <td key={k} className="num">{d[k] || "—"}</td>)}
          <td className="num"><strong>{d.alerts}</strong></td><td className="num">{d.quiet || "—"}</td><td className="num">{d.quiet_external || "—"}</td></tr>)}</tbody>
      </table></div>
      {!!data.noisy.length && <p className="muted">Lo que más casos genera sin avisar: {data.noisy.map((n: any) => `${n.subject} (${n.cases})`).join(", ")}.</p>}
      <p className="muted">Análisis de ODIN: {data.reviews.analyses} hechos, {reviewed} valorados por el NOC
        {reviewed ? ` (${data.reviews.correct || 0} correctos, ${data.reviews.partial || 0} parciales, ${data.reviews.incorrect || 0} incorrectos)` : ""}.
        Un caso se cierra tras {t.quiet_close_minutes} min estable y pasa a «sin actividad» tras {t.stale_hours} h sin cambios.</p>
    </>}
  </details>;
}
