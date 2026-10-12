import { labels } from "./probes";
import { Verdict } from "./monitoring";
type Item = {
  kind: string; id: string; link: string | null; severity: string; title: string; where: string; detail: string; at: number | null;
  [key: string]: any;
};
// Stages of an analysis run (analyst/service.py).
const stages: Record<string, string> = {
  pending: "en cola", gathering: "reuniendo evidencias", reading: "leyendo evidencias", writing: "redactando",
  validating: "comprobando referencias", delayed: "esperando al modelo", running: "en curso",
};
const kinds: Record<string, string> = {
  case: "Caso", incident: "Incidente", olt_outage: "Caída de PON", olt: "OLT", review: "Diagnóstico", health: "ODIN",
  analysis: "Análisis", note: "Proveedor", case_closed: "Caso cerrado", incident_recovered: "Incidente recuperado",
  olt_outage_ended: "PON restablecido",
};
function length(seconds: number) {
  seconds = Math.max(0, Math.round(seconds));
  if (seconds < 3600) return `${Math.max(1, Math.round(seconds / 60))} min`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ${Math.round((seconds % 3600) / 60)} min`;
  return `${Math.floor(seconds / 86400)} d ${Math.floor((seconds % 86400) / 3600)} h`;
}
const ago = (seconds: number) => (seconds < 60 ? "hace un momento" : "hace " + length(seconds));
const when = (s: number | null | undefined, zone: string) =>
  s ? new Date(s * 1000).toLocaleString("es", { timeZone: zone, dateStyle: "short", timeStyle: "short" }) : "";

function Row({ item, now, zone, onNavigate, children }: { item: Item; now: number; zone: string; onNavigate: (page: string) => void; children?: any }) {
  const open = item.link ? (
    <a className="inbox-open" href={item.link ?? undefined}>Abrir</a>
  ) : item.page ? (
    <button className="quiet inbox-open" onClick={() => onNavigate(item.page)}>Ver {item.page}</button>
  ) : null;
  const detail = item.kind === "incident" || item.kind === "incident_recovered"
    ? (item.conditions || []).map((c: string) => labels[c] || c).join(", ")
    : item.kind === "analysis" ? stages[item.detail] || item.detail : item.detail;
  return (
    <li className={"inbox-row " + item.severity}>
      <span className="inbox-kind">{kinds[item.kind] || item.kind}</span>
      <div className="inbox-main">
        <strong>{item.kind === "review" && item.case_title ? item.case_title : item.title}</strong>
        <small>
          {item.where && <span className={/^\d|:/.test(item.where) ? "mono" : ""}>{item.where}</span>}
          {item.where && detail ? " · " : ""}
          {detail}
        </small>
        {children}
      </div>
      <span className="inbox-time" title={when(item.at, zone)}>{item.at ? ago(now - item.at) : ""}</span>
      {open}
    </li>
  );
}

function Section({ title, hint, items, empty, children }: { title: string; hint?: string; items: Item[]; empty: string; children: (i: Item) => any }) {
  return (
    <section className="panel inbox-section">
      <div className="title-row">
        <h2>{title} <span className="inbox-count">{items.length}</span></h2>
        {hint && <small>{hint}</small>}
      </div>
      {items.length ? <ul className="inbox-list">{items.map(children)}</ul> : <p className="muted">{empty}</p>}
    </section>
  );
}

/** What ODIN needs from the NOC now, what is in progress and what was resolved in the shift. */
export function Inbox({ data, zone, onNavigate }: { data: any; zone: string; onNavigate: (page: string) => void }) {
  if (!data) return <p className="muted">Consultando la bandeja…</p>;
  const now = data.at;
  return (
    <>
      <div className="inbox-health" aria-label="Salud de ODIN">
        {data.health.map((c: any) => (
          <span key={c.id} className={"inbox-chip " + c.state} title={c.titles.join(" · ") || "Sin avisos"}>
            <span className="status-dot" aria-hidden="true" />
            {c.label}
          </span>
        ))}
      </div>
      <Verdict verdict={data.verdict} />
      <Section title="Necesita de ti" items={data.attention} empty="Nada pendiente. Si algo cambia, aparece aquí primero."
        hint="Lo crítico primero; dentro de cada nivel, lo más antiguo.">
        {(i) => <Row key={i.kind + i.id} item={i} now={now} zone={zone} onNavigate={onNavigate} />}
      </Section>
      <div className="inbox-columns">
        <Section title="En curso" items={data.progress} empty="Nadie está atendiendo nada en este momento.">
          {(i) => (
            <Row key={i.kind + i.id} item={i} now={now} zone={zone} onNavigate={onNavigate}>
              {i.acknowledged_by && <small>Reconocido por {i.acknowledged_by} {ago(now - i.acknowledged_at)}</small>}
            </Row>
          )}
        </Section>
        <Section title={`Resuelto en el turno`} hint={`Últimas ${data.shift_hours} h`} items={data.resolved} empty="Nada resuelto en el turno todavía.">
          {(i) => (
            <Row key={i.kind + i.id} item={{ ...i, severity: "ok", at: i.closed_at || i.at }} now={now} zone={zone} onNavigate={onNavigate}>
              <small>
                Duró {length((i.closed_at || now) - (i.at || i.closed_at || now))}
                {i.analysis_id && !i.rated && <> · <a href={i.link ?? undefined}>Valora el diagnóstico</a></>}
                {i.rated && " · diagnóstico valorado"}
              </small>
            </Row>
          )}
        </Section>
      </div>
    </>
  );
}
