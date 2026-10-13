import { FormEvent, Fragment, useEffect, useRef, useState } from "react";

type Api = (path: string, method?: string, body?: unknown) => Promise<any>;
// A case, or a POP whose equipment limits a period question.
export type ChatCase = { id: string; label: string; kind?: "case" | "pop" };
type Links = { onOpenCase: (id: string) => void; onOpenIncident: (id: string) => void; onGo: (page: string) => void };

const periods: [string, string, number][] = [["1h", "Última hora", 3600], ["6h", "Últimas 6 horas", 21600], ["24h", "Últimas 24 horas", 86400], ["7d", "Últimos 7 días", 604800]];
const stages: Record<string, string> = {
  queued: "En cola", planning: "Entendiendo la pregunta", gathering: "Consultando la red", reading: "Leyendo el contexto", writing: "Preparando la respuesta",
  validating: "Comprobando referencias", delayed: "El modelo no responde; se reintentará",
};
const errors: Record<string, string> = {
  invalid_output: "El modelo no devolvió una respuesta válida", unknown_references: "La respuesta citaba datos que no existen y se descartó",
  output_truncated: "La respuesta superó el límite de longitud", connection_failed: "No se pudo conectar con el modelo", llm_timeout: "El modelo no respondió a tiempo",
  server_unavailable: "El modelo no está disponible", analyst_not_configured: "El analista no está activado", pack_too_large: "El contexto no cabe; acorta el período",
  cancelled: "Cancelada",
};

function localInput(stamp: number) {
  const d = new Date(stamp * 1000);
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

export function Chat({ api, zone, initialCase, links }: { api: Api; zone: string; initialCase: ChatCase | null; links: Links }) {
  const [list, setList] = useState<any[]>([]);
  const [current, setCurrent] = useState<any>(null);
  const [error, setError] = useState("");
  const [text, setText] = useState("");
  const [period, setPeriod] = useState("6h");
  const [custom, setCustom] = useState(() => ({ start: localInput(Date.now() / 1000 - 21600), end: localInput(Date.now() / 1000) }));
  const [context, setContext] = useState<ChatCase | null>(initialCase);
  const [live, setLive] = useState<any>(null);
  const [status, setStatus] = useState<any>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const pop = context?.kind === "pop";
  const periodic = !context || pop;
  const date = (stamp: number | null) => stamp == null ? "—" : new Date(stamp * 1000).toLocaleString("es", { timeZone: zone, dateStyle: "short", timeStyle: "short" });
  const loadList = () => api("/conversations").then(setList).catch((e) => setError((e as Error).message));
  const open = (id: string) => api("/conversations/" + id).then((c) => { setCurrent(c); setError(""); }).catch((e) => setError((e as Error).message));
  useEffect(() => { void loadList(); api("/analyst/status").then(setStatus).catch(() => setStatus(null)); }, [api]);
  useEffect(() => { if (initialCase) { setContext(initialCase); setCurrent(null); } }, [initialCase]);
  const pending = current?.messages.find((m: any) => m.role === "user" && m.run && (m.run.status === "pending" || m.run.status === "running"));
  // Progress arrives by server-sent events; the conversation reloads when the answer is ready.
  useEffect(() => {
    if (!pending) { setLive(null); return; }
    const source = new EventSource(`/api/v1/chat-runs/${pending.run.id}/events`);
    source.onmessage = (event) => {
      const state = JSON.parse(event.data);
      setLive(state);
      if (state.status !== "pending" && state.status !== "running") { source.close(); void open(current.id); void loadList(); }
    };
    source.addEventListener("gone", () => source.close());
    source.onerror = () => { source.close(); window.setTimeout(() => void open(current.id), 5000); };
    // Safety net if events are held back somewhere on the way.
    const timer = window.setInterval(() => void open(current.id), 20000);
    return () => { source.close(); window.clearInterval(timer); };
  }, [pending?.run.id]);
  useEffect(() => { bottom.current?.scrollIntoView({ block: "end" }); }, [current?.messages.length, live?.stage]);
  const summary = "Resume lo ocurrido en el período: casos, incidentes de latencia, alertas relevantes y cambios de configuración. Termina con la cobertura de las fuentes y lo que quede pendiente de revisar.";
  const send = async (e: FormEvent | null, content = text) => {
    e?.preventDefault();
    const now = Math.floor(Date.now() / 1000);
    const chosen = periods.find(([key]) => key === period);
    const start = chosen ? now - chosen[2] : Math.floor(new Date(custom.start).getTime() / 1000);
    const end = chosen ? now : Math.floor(new Date(custom.end).getTime() / 1000);
    try {
      const conversation = current || await api("/conversations", "POST", {});
      await api(`/conversations/${conversation.id}/messages`, "POST", { client_id: crypto.randomUUID(), content, start, end, case_id: pop ? null : context?.id || null, pop_id: pop ? context?.id : null });
      setText(""); await open(conversation.id); void loadList();
    } catch (err) { setError((err as Error).message); }
  };
  const act = async (fn: () => Promise<unknown>) => { try { await fn(); if (current) await open(current.id); void loadList(); } catch (e) { setError((e as Error).message); } };
  return <div className="chat-layout">
    <section className="panel chat-list">
      <button onClick={() => { setCurrent(null); setContext(null); }}>Nueva conversación</button>
      {!list.length && <p className="muted">Sin conversaciones. Son privadas: nadie más las ve, tampoco los administradores.</p>}
      {list.map((c) => <button key={c.id} className={"link" + (current?.id === c.id ? " selected" : "")} aria-current={current?.id === c.id} onClick={() => void open(c.id)}>
        {c.title}<small> · {date(c.updated_at)}</small></button>)}
    </section>
    <section className="panel chat-main">
      <div className="title-row"><h2>{current?.title || "Nueva conversación"}</h2>
        {current && <button className="quiet" onClick={() => { if (window.confirm("¿Eliminar esta conversación? Los casos y evidencias no se borran.")) void api("/conversations/" + current.id, "DELETE").then(() => { setCurrent(null); void loadList(); }).catch((e) => setError((e as Error).message)); }}>Eliminar</button>}
      </div>
      {status && !status.enabled && <p className="notice">El analista no está activado. Un administrador lo activa en Casos.</p>}
      {!current && <p className="muted">Pregunta por un período («¿Qué pasó en las últimas 6 horas?»), por un caso o por un POP (desde el inventario). ODIN responde con los casos, incidentes, alertas y logs que tiene, cita cada dato y avisa de las fuentes que no cubren el período. No ejecuta nada sobre la red.</p>}
      <div className="chat-messages">
        {current?.messages.map((m: any) => m.role === "user"
          ? <div key={m.id} className="chat-question"><p>{m.content}</p><small className="muted">{date(m.created_at)}
              {m.run?.request && ` · período ${date(m.run.request.start)} — ${date(m.run.request.end)}`}{m.run?.request?.case_id && " · sobre un caso"}{m.run?.request?.pop_id && " · sobre un POP"}</small>
              {m.run && (m.run.status === "failed" || m.run.status === "cancelled") && <p role="alert">{errors[m.run.error] || m.run.error || "Sin respuesta"}.{" "}
                <button className="quiet" onClick={() => void act(() => api(`/conversations/${current.id}/messages/${m.id}/retry`, "POST"))}>Reintentar</button></p>}
            </div>
          : <Answer key={m.id} message={m} run={m.run} links={links} />)}
        {pending && <p role="status" className="chat-progress">
          {stages[live?.stage || pending.run.stage] || "Procesando"}
          {(live?.queue_position || pending.run.queue_position) > 1 && ` · posición ${live?.queue_position || pending.run.queue_position} en la cola`}
          {live?.stage === "reading" && live.progress?.total ? ` · ${Math.round(100 * live.progress.done / live.progress.total)} %` : ""}
          {" "}<button className="quiet" onClick={() => void act(() => api(`/analyses/${pending.run.id}/cancel`, "POST"))}>Cancelar</button></p>}
        <div ref={bottom} />
      </div>
      <form className="chat-form" onSubmit={send}>
        {context && <p><span className="badge">Contexto: {context.label}</span> <button type="button" className="quiet" onClick={() => setContext(null)}>Quitar</button></p>}
        <textarea value={text} maxLength={2000} rows={3} onChange={(e) => setText(e.target.value)} required placeholder="Escribe tu consulta…"
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); (e.currentTarget.form as HTMLFormElement).requestSubmit(); } }} />
        <div className="event-filters">
          {periodic && <label>Período<select value={period} onChange={(e) => setPeriod(e.target.value)}>
            {periods.map(([key, label]) => <option key={key} value={key}>{label}</option>)}<option value="custom">Personalizado</option>
          </select></label>}
          {periodic && period === "custom" && <>
            <label>Desde<input type="datetime-local" value={custom.start} onChange={(e) => setCustom({ ...custom, start: e.target.value })} required /></label>
            <label>Hasta<input type="datetime-local" value={custom.end} onChange={(e) => setCustom({ ...custom, end: e.target.value })} required /></label></>}
          <button disabled={!!pending || !text.trim()}>Enviar</button>
          {periodic && <button type="button" className="quiet" disabled={!!pending} onClick={() => void send(null, summary)}>Resumen del período</button>}
        </div>
      </form>
      {error && <p role="alert">{error}</p>}
    </section>
  </div>;
}

function Answer({ message, run, links }: { message: any; run: any; links: Links }) {
  const refs = run?.refs || {};
  const follow = (label: string) => {
    const ref = refs[label];
    if (!ref) return;
    if (ref.kind === "case") links.onOpenCase(ref.ids[0]);
    else if (ref.kind === "incident") links.onOpenIncident(ref.ids[0]);
    else if (ref.kind === "evidence" || ref.kind === "note") { if (run.request?.case_id) links.onOpenCase(run.request.case_id); }
    else if (ref.kind === "events" || ref.kind === "logs") links.onGo("Eventos");
    else if (ref.kind === "knowledge") links.onGo("Casos");
    else if (ref.kind === "probe") links.onGo("Servicios externos");
    else if (ref.kind === "smartolt") links.onGo("OLTs");
  };
  // Labels become buttons to the cited data; lines starting with "- " become a list.
  const inline = (line: string) => line.split(/(\[[A-Z][0-9]{1,3}(?:, *[A-Z][0-9]{1,3})*\]|\*\*[^*]+\*\*)/).map((part, i) => {
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) return <strong key={i}>{part.slice(2, -2)}</strong>;
    const labels = part.match(/^\[([A-Z][0-9]{1,3}(?:, *[A-Z][0-9]{1,3})*)\]$/)?.[1].split(/, */);
    return labels ? <Fragment key={i}>{labels.map((label) => <button key={label} className="chip ref-chip" title={refs[label] ? `${refs[label].count} elemento(s)` : ""}
      onClick={() => follow(label)}>{label}</button>)}</Fragment> : <Fragment key={i}>{part}</Fragment>;
  });
  const lines = message.content.split("\n").filter((l: string) => l.trim());
  return <div className="chat-answer">
    {lines.map((line: string, i: number) => line.startsWith("- ") ? <p key={i} className="chat-item">• {inline(line.slice(2))}</p> : <p key={i}>{inline(line)}</p>)}
    {!!run?.result?.datos_faltantes?.length && <><strong>Datos faltantes</strong><ul className="case-impact">{run.result.datos_faltantes.map((d: string) => <li key={d}>{d}</li>)}</ul></>}
    {run && <details><summary>Fuentes y cobertura</summary><ul className="case-impact">{run.coverage.map((c: string) => <li key={c}>{c}</li>)}</ul>
      {!!run.omitted.length && <p className="muted">No se incluyeron por espacio: {run.omitted.join("; ")}.</p>}
      <p className="muted">{run.model?.split("/").pop()} · {run.pack_tokens} tokens de contexto · {Math.round((run.duration_ms || 0) / 1000)} s</p></details>}
  </div>;
}
