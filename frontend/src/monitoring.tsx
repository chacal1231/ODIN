import { useEffect, useState, useId, useRef, useCallback } from "react";
import { Paged } from "./pager";
import { ProbeBadge, labels } from "./probes";
import { reasons } from "./events";
type Api = (path: string, method?: string, body?: unknown) => Promise<any>;
const stateOrder: Record<string, number> = {
  critical: 0,
  partial: 1,
  warning: 2,
  no_data: 3,
  pending_validation: 4,
  warming: 5,
  maintenance: 6,
  paused: 7,
  unmonitored: 8,
  healthy: 9,
};
const affectedStates = ["critical", "warning", "partial"];
// A snapshot older than three polling rounds must not look like a calm network.
const STALE_SECONDS = 45;
function groupState(items: any[]) {
  if (!items.length) return "unmonitored";
  const affected = items.filter((x) => affectedStates.includes(x.state));
  if (
    affected.length &&
    (affected.length < items.length ||
      affected.some((x) => x.state === "partial"))
  )
    return "partial";
  return [...items].sort(
    (a, b) => (stateOrder[a.state] ?? 3) - (stateOrder[b.state] ?? 3),
  )[0].state;
}
function ProviderSummary({ members }: { members: { state: string }[] }) {
  const groups = [
    { state: "healthy", states: ["healthy"], one: "normal", many: "normales" },
    { state: "warning", states: affectedStates, one: "con problemas", many: "con problemas" },
    { state: "no_data", states: ["no_data"], one: "sin datos", many: "sin datos" },
    { state: "pending_validation", states: ["pending_validation"], one: "pendiente de validación", many: "pendientes de validación" },
    { state: "warming", states: ["warming"], one: "reuniendo muestras", many: "reuniendo muestras" },
    { state: "maintenance", states: ["maintenance"], one: "en mantenimiento", many: "en mantenimiento" },
    { state: "paused", states: ["paused"], one: "pausado", many: "pausados" },
    { state: "unmonitored", states: ["unmonitored"], one: "sin comprobaciones esenciales", many: "sin comprobaciones esenciales" },
  ];
  const unknown = members.filter((m) => !groups.some((g) => g.states.includes(m.state))).length;
  return (
    <span className="provider-summary">
      {groups.map((group) => {
        const count = members.filter((m) => group.states.includes(m.state)).length;
        if (!count) return null;
        const severity = group.state === "warning" && members.some((m) => m.state === "critical")
          ? "critical" : group.state;
        return <span key={group.state} className={"badge probe-status " + severity}>
          {count} {count === 1 ? group.one : group.many}
        </span>;
      })}
      {unknown > 0 && <span className="badge probe-status no_data">{unknown} sin estado conocido</span>}
    </span>
  );
}
const fmt = (n: number | null | undefined, d = 1) =>
  n == null ? "—" : n.toLocaleString("es", { maximumFractionDigits: d });
// Host or IP each check measures; a name shows the addresses it resolved to.
const targets = (checks: any[]) =>
  [...new Set(checks.map((c: any) => {
    const ips = (c.resolved_ips || []).filter((ip: string) => ip !== c.address);
    return ips.length ? `${c.address} → ${ips.join(", ")}` : c.address;
  }))].join(" · ");
export function ago(seconds: number) {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return s + " s";
  const m = Math.floor(s / 60);
  if (m < 60) return m + " min";
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} h ${m % 60} min`;
  return `${Math.floor(h / 24)} d ${h % 24} h`;
}
function useNow(ms: number) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(timer);
  }, [ms]);
  return now;
}
const lossConditions = [
  "loss",
  "availability",
  "unreachable",
  "endpoint_unavailable",
];
const worse = (a: string, b: string) =>
  a === "critical" || b === "critical" ? "critical" : a || b;
function level(conditions: Record<string, string> | undefined, keys: string[]) {
  return keys.reduce((acc, k) => worse(acc, conditions?.[k] || ""), "");
}
type Metrics = {
  p95: number | null;
  loss: number | null;
  latency: string;
  lossLevel: string;
};
/** Worst current reading across fresh measurements; stale ones are ignored. */
function checkMetrics(checks: any[]): Metrics {
  const out: Metrics = { p95: null, loss: null, latency: "", lossLevel: "" };
  for (const c of checks)
    for (const m of c.measurements || []) {
      if (m.state === "no_data") continue;
      if (m.p95_ms != null && (out.p95 == null || m.p95_ms > out.p95))
        out.p95 = m.p95_ms;
      const loss =
        (c.protocol || "icmp") === "icmp" ? m.loss_percent : m.failure_percent;
      if (loss != null && (out.loss == null || loss > out.loss)) out.loss = loss;
      out.latency = worse(out.latency, level(m.conditions, ["latency", "jitter"]));
      out.lossLevel = worse(out.lossLevel, level(m.conditions, lossConditions));
    }
  return out;
}
function destinationMetrics(d: any) {
  const essential = d.checks.filter((c: any) => c.essential);
  return checkMetrics(essential.length ? essential : d.checks);
}
function Metric({
  value,
  unit,
  level,
}: {
  value: number | null;
  unit: string;
  level?: string;
}) {
  return (
    <span className={"metric " + (level || "")}>
      {value == null ? "—" : fmt(value) + " " + unit}
    </span>
  );
}

export type Monitor = {
  data: any;
  updatedAt: number | null;
  failed: boolean;
  reload: () => Promise<void>;
};
export function useMonitoring(api: Api, enabled: boolean): Monitor {
  const [data, setData] = useState<any>(null),
    [updatedAt, setUpdatedAt] = useState<number | null>(null),
    [failed, setFailed] = useState(false);
  const reload = useCallback(async () => {
    try {
      const next = await api("/monitoring");
      setData(next);
      setUpdatedAt(Date.now());
      setFailed(false);
    } catch {
      // Keep the last snapshot visible; the status bar marks it as stale.
      setFailed(true);
    }
  }, [api]);
  useEffect(() => {
    if (!enabled) {
      setData(null);
      setUpdatedAt(null);
      setFailed(false);
      return;
    }
    void reload();
    const timer = setInterval(() => void reload(), 15000);
    return () => clearInterval(timer);
  }, [enabled, reload]);
  return { data, updatedAt, failed, reload };
}
function freshness(monitor: Monitor, now: number) {
  const age =
    monitor.updatedAt == null ? null : (now - monitor.updatedAt) / 1000;
  return { age, stale: monitor.failed || age == null || age > STALE_SECONDS };
}

export function StatusBar({
  monitor,
  onNavigate,
}: {
  monitor: Monitor;
  onNavigate: (page: string) => void;
}) {
  const now = useNow(1000);
  const { data, failed } = monitor;
  const { age, stale } = freshness(monitor, now);
  const destinations = data?.destinations || [];
  const count = (states: string[]) =>
    data
      ? destinations.filter((d: any) => states.includes(d.state)).length
      : null;
  const critical = count(["critical"]),
    degraded = count(["warning", "partial"]),
    noData = count(["no_data"]);
  const items = (data?.incident_groups || []).flatMap((g: any) => g.items);
  const unacknowledged = items.filter((i: any) => !i.acknowledged).length;
  const health = data?.odin_health;
  const issues: any[] = health?.checks || [];
  const [healthOpen, setHealthOpen] = useState(false);
  // A probe that skips most rounds leaves stale measurements; green would mislead.
  const probeBehind = issues.some((c) => c.id === "probe_rate" && c.state === "critical");
  const [state, label] = !data
    ? failed
      ? ["no_data", "Sin conexión con ODIN"]
      : ["no_data", "Consultando…"]
    : stale || data.origin.state !== "healthy" || probeBehind
      ? ["no_data", "Sin datos fiables"]
      : critical || items.some((i: any) => i.severity === "critical")
        ? ["critical", "Afectación crítica"]
        : degraded || items.length
          ? ["warning", "Degradación"]
          : ["healthy", "Operación normal"];
  const counter = (
    value: number | null,
    text: string,
    tone: string,
    page: string,
  ) => (
    <button
      className={"status-count " + tone + (value ? "" : " zero")}
      onClick={() => onNavigate(page)}
    >
      <b>{value ?? "—"}</b> {text}
    </button>
  );
  return (
    <section className={"status-bar " + state} aria-label="Estado de la red">
      <strong className="status-overall" role="status">
        <span className="status-dot" aria-hidden="true" />
        {label}
      </strong>
      {counter(critical, "críticos", "critical", "Inicio")}
      {counter(degraded, "degradados", "warning", "Inicio")}
      {counter(noData, "sin datos", "no_data", "Inicio")}
      <button
        className={"status-count" + (items.length ? "" : " zero")}
        onClick={() => onNavigate("Incidentes")}
      >
        <b>{data ? items.length : "—"}</b> incidentes
        {unacknowledged > 0 && (
          <span className="unack"> · {unacknowledged} sin reconocer</span>
        )}
      </button>
      {data?.cases?.alerts_enabled && (
        <button
          className={"status-count " + (data.cases.critical ? "critical" : data.cases.active ? "warning" : "zero")}
          onClick={() => onNavigate("Casos")}
        >
          <b>{data.cases.active}</b> casos
          {data.cases.unacknowledged > 0 && <span className="unack"> · {data.cases.unacknowledged} sin reconocer</span>}
        </button>
      )}
      {health && (
        <button
          className={"status-count status-odin " + (issues.length ? health.state : "zero")}
          aria-expanded={issues.length ? healthOpen : undefined}
          disabled={!issues.length}
          onClick={() => setHealthOpen(!healthOpen)}
        >
          ODIN{" "}
          {issues.length ? (
            <>
              · <b>{issues.length}</b> {issues.length === 1 ? "aviso" : "avisos"}
            </>
          ) : (
            "operativo"
          )}
        </button>
      )}
      <span className="status-origin">
        Sondeador <ProbeBadge state={data?.origin.state || "no_data"} />
      </span>
      <span className={"status-age" + (stale ? " stale" : "")}>
        {failed && data ? "Sin conexión · " : ""}
        {age == null ? "Sin actualizar" : "hace " + ago(age)}
      </span>
      {healthOpen && issues.length > 0 && (
        <ul className="odin-health" aria-label="Avisos de ODIN">
          {issues.map((c) => (
            <li key={c.id} className={c.state}>
              <strong>{c.title}</strong>
              <span>
                {c.detail}
                {c.error_code && " Último error: " + (reasons[c.error_code] || c.error_code) + "."}
              </span>
              <button
                className="link"
                onClick={() => {
                  setHealthOpen(false);
                  onNavigate(c.page);
                }}
              >
                Ver {c.page}
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function PopGrid({
  data,
  selected,
  onSelect,
}: {
  data: any;
  selected?: string;
  onSelect: (key: string) => void;
}) {
  const network = (data?.destinations || []).filter(
    (d: any) => d.scope === "network",
  );
  return (
    <div className="pop-grid">
      {(data?.pops || []).map((p: any) => {
        const key = p.id || "unassigned";
        const members = network.filter(
          (d: any) => (d.pop_id || "unassigned") === key,
        );
        const affected = members.filter((d: any) =>
          affectedStates.includes(d.state),
        ).length;
        return (
          <button
            key={key}
            className={
              "pop-tile state-" + p.state + (selected === key ? " selected" : "")
            }
            aria-pressed={selected === undefined ? undefined : selected === key}
            onClick={() => onSelect(key)}
          >
            <span className="pop-tile-head">
              <strong>{p.name}</strong>
              <ProbeBadge state={p.state} />
            </span>
            <span className="pop-tile-figure">
              <b>{affected}</b> / {members.length} destinos afectados
            </span>
            <small>
              {p.incidents} incidentes · {p.monitored_devices}/{p.devices}{" "}
              equipos con sondeo
            </small>
          </button>
        );
      })}
    </div>
  );
}

type QueueRow = Metrics & {
  key: string;
  severity: string;
  name: string;
  check: string;
  address: string;
  place: string;
  conditions: string[];
  since: number | null;
  ack: boolean | null;
  incidentId?: string;
  destinationId: string;
  scope: string;
  essential: boolean;
};
const severityRank: Record<string, number> = {
  critical: 0,
  warning: 1,
  no_data: 2,
};
const ackRank = (a: boolean | null) => (a === false ? 0 : a === null ? 1 : 2);
function attentionRows(data: any): QueueRow[] {
  if (!data) return [];
  const checks = new Map<string, { check: any; dest: any }>();
  for (const d of data.destinations)
    for (const c of d.checks) checks.set(c.id, { check: c, dest: d });
  const place = (d: any) =>
    d.scope === "network"
      ? d.pop_name + (d.device_name ? " · " + d.device_name : "")
      : d.provider + " · " + d.region;
  const rows: QueueRow[] = [],
    withIncident = new Set<string>();
  for (const g of data.incident_groups)
    for (const i of g.items) {
      withIncident.add(i.target_id);
      const found = checks.get(i.target_id);
      const metrics = found
        ? checkMetrics([
            {
              ...found.check,
              measurements: found.check.measurements.filter(
                (m: any) => m.address === i.address,
              ),
            },
          ])
        : { p95: null, loss: null, latency: "", lossLevel: "" };
      rows.push({
        ...metrics,
        key: i.id,
        severity: i.severity,
        name: found?.dest.name || g.name,
        check: i.target_name,
        address: i.address,
        place: found ? place(found.dest) : g.scope === "network" ? g.name : "—",
        conditions: Object.keys(i.conditions || {}),
        since: i.opened_at,
        ack: i.acknowledged,
        incidentId: i.id,
        destinationId: i.destination_id,
        scope: g.scope,
        essential: found?.check.essential ?? true,
      });
    }
  // With the prober down every check reads no_data; the status bar reports that once.
  const originHealthy = data.origin.state === "healthy";
  for (const { check: c, dest: d } of checks.values()) {
    if (withIncident.has(c.id)) continue;
    const affected = c.state === "critical" || c.state === "warning";
    const missing = c.state === "no_data" && c.enabled && originHealthy;
    if (!affected && !missing) continue;
    const slots = c.measurements
      .map((m: any) => m.last_slot)
      .filter((s: any) => s != null);
    rows.push({
      ...checkMetrics([c]),
      key: c.id,
      severity: c.state,
      name: d.name,
      check: c.name,
      address: c.address,
      place: place(d),
      conditions: missing
        ? ["no_data"]
        : [
            ...new Set<string>(
              c.measurements.flatMap((m: any) => Object.keys(m.conditions || {})),
            ),
          ],
      since: missing && slots.length ? Math.max(...slots) : null,
      ack: null,
      destinationId: d.id,
      scope: d.scope,
      essential: c.essential,
    });
  }
  return rows.sort(
    (a, b) =>
      (severityRank[a.severity] ?? 3) - (severityRank[b.severity] ?? 3) ||
      ackRank(a.ack) - ackRank(b.ack) ||
      (a.since ?? Infinity) - (b.since ?? Infinity) ||
      a.name.localeCompare(b.name),
  );
}
function AttentionQueue({
  data,
  zone,
  onOpenIncident,
  onOpenDestination,
}: {
  data: any;
  zone: string;
  onOpenIncident: (id: string) => void;
  onOpenDestination: (scope: string, id: string) => void;
}) {
  const now = useNow(5000) / 1000;
  const rows = attentionRows(data);
  if (!data) return <p className="muted">Consultando el monitoreo…</p>;
  if (!rows.length)
    return (
      <p className="muted">
        Sin incidentes activos ni comprobaciones degradadas o sin datos.
      </p>
    );
  return (
    <div className="probe-table">
      <table className="noc-table">
        <thead>
          <tr>
            <th>Severidad</th>
            <th>Destino</th>
            <th>Ubicación</th>
            <th>Condición</th>
            <th className="num">Latencia p95</th>
            <th className="num">Pérdida / fallos</th>
            <th>Desde</th>
            <th>Reconocimiento</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key} className={"sev-" + r.severity}>
              <td>
                <ProbeBadge state={r.severity} />
              </td>
              <td>
                <button
                  className="link"
                  onClick={() =>
                    r.incidentId
                      ? onOpenIncident(r.incidentId)
                      : onOpenDestination(r.scope, r.destinationId)
                  }
                >
                  {r.name}
                </button>
                <small>
                  {r.check !== r.name ? r.check + " · " : ""}
                  <span className="mono">{r.address}</span>
                  {r.essential ? "" : " · opcional"}
                </small>
              </td>
              <td>{r.place}</td>
              <td>
                {r.conditions.map((c) => labels[c] || c).join(", ") || "—"}
              </td>
              <td className="num">
                <Metric value={r.p95} unit="ms" level={r.latency} />
              </td>
              <td className="num">
                <Metric value={r.loss} unit="%" level={r.lossLevel} />
              </td>
              <td
                className="num"
                title={
                  r.since
                    ? new Date(r.since * 1000).toLocaleString("es", {
                        timeZone: zone,
                      })
                    : undefined
                }
              >
                {r.since ? ago(now - r.since) : "—"}
              </td>
              <td>
                {r.ack === null ? (
                  <small>
                    {r.severity === "no_data"
                      ? "Sin medición reciente"
                      : "Sin incidente abierto"}
                  </small>
                ) : r.ack ? (
                  "Reconocido"
                ) : (
                  <span className="unack">Sin reconocer</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const chips: [string, string, (d: any) => boolean][] = [
  ["all", "Todos", () => true],
  [
    "attention",
    "Con novedades",
    (d) => d.state !== "healthy" || d.optional_issues > 0,
  ],
  ["critical", "Críticos", (d) => d.state === "critical"],
  ["degraded", "Degradados", (d) => ["warning", "partial"].includes(d.state)],
  ["no_data", "Sin datos", (d) => d.state === "no_data"],
  [
    "inactive",
    "Pausa, mantenimiento y otros",
    (d) =>
      [
        "paused",
        "maintenance",
        "pending_validation",
        "warming",
        "unmonitored",
      ].includes(d.state),
  ],
  ["healthy", "Normal", (d) => d.state === "healthy"],
];
type SortKey = "state" | "name" | "p95" | "loss" | "incidents";
export function MonitoringNavigator({
  data,
  scope,
  selected,
  onSelect,
  onAdd,
  onConfigure,
  admin,
  focus,
}: {
  focus?: { popId?: string; linkId?: string };
  data: any;
  scope: string;
  selected: string;
  onSelect: (id: string) => void;
  onAdd: (d: any) => void;
  onConfigure: (d: any) => void;
  admin: boolean;
}) {
  const network = scope === "network";
  const [query, setQuery] = useState(""),
    [category, setCategory] = useState(""),
    [parent, setParent] = useState(focus?.popId || ""),
    [status, setStatus] = useState("all"),
    [sort, setSort] = useState<{ key: SortKey; dir: number }>({
      key: "state",
      dir: 1,
    }),
    [openGroups, setOpenGroups] = useState<Record<string, boolean>>({}),
    [tab, setTab] = useState(focus?.linkId ? "links" : "destinations"),
    [linkFocus, setLinkFocus] = useState(focus?.linkId || "");
  const scoped = (data?.destinations || []).filter(
    (d: any) => d.scope === scope,
  );
  const categories = [
    ...new Set<string>(scoped.flatMap((d: any) => d.categories)),
  ].sort();
  const metrics = new Map<string, Metrics>(
    scoped.map((d: any) => [d.id, destinationMetrics(d)]),
  );
  const base = scoped.filter(
    (d: any) =>
      (!category || d.categories.includes(category)) &&
      (!network || !parent || (d.pop_id || "unassigned") === parent) &&
      [
        d.name,
        d.provider,
        d.region,
        d.device_name,
        d.pop_name,
        ...d.checks.flatMap((c: any) => [
          c.address,
          c.catalog_metadata?.city,
          c.catalog_metadata?.country,
        ]),
      ]
        .join(" ")
        .toLowerCase()
        .includes(query.toLowerCase()),
  );
  const chipTest = chips.find(([key]) => key === status)![2];
  const sortValue = (d: any): number | string => {
    const m = metrics.get(d.id)!;
    if (sort.key === "state") return stateOrder[d.state] ?? 3;
    if (sort.key === "name") return d.name;
    if (sort.key === "p95") return m.p95 ?? -1;
    if (sort.key === "loss") return m.loss ?? -1;
    return d.incidents;
  };
  const visible = base.filter(chipTest).sort((a: any, b: any) => {
    const x = sortValue(a),
      y = sortValue(b);
    const order =
      typeof x === "string"
        ? x.localeCompare(y as string)
        : x - (y as number);
    return order * sort.dir || a.name.localeCompare(b.name);
  });
  const selectedGroup = data?.destinations?.find((d: any) => d.id === selected);
  // Arriving from the attention queue: bring the chosen row into view once.
  const scrolled = useRef(false);
  useEffect(() => {
    if (scrolled.current || !selected || !data) return;
    scrolled.current = true;
    requestAnimationFrame(() =>
      document
        .querySelector(".noc-table tr.selected")
        ?.scrollIntoView({ block: "center" }),
    );
  }, [data, selected]);
  function reset(fn: () => void) {
    fn();
    onSelect("");
  }
  function toggleSort(key: SortKey) {
    setSort((s) =>
      s.key === key
        ? { key, dir: -s.dir }
        : { key, dir: key === "state" || key === "name" ? 1 : -1 },
    );
  }
  const header = (key: SortKey, label: string, num = false) => (
    <th
      className={num ? "num" : ""}
      aria-sort={
        sort.key === key
          ? sort.dir === 1
            ? "ascending"
            : "descending"
          : "none"
      }
    >
      <button className="th-sort" onClick={() => toggleSort(key)}>
        {label}
        {sort.key === key ? (sort.dir === 1 ? " ▲" : " ▼") : ""}
      </button>
    </th>
  );
  const columns = 8;
  function row(d: any) {
    const m = metrics.get(d.id)!;
    return (
      <tr
        key={d.id}
        className={(selected === d.id ? "selected " : "") + "sev-" + d.state}
      >
        <td>
          <ProbeBadge state={d.state} />
        </td>
        <td>
          <button
            className="link"
            aria-expanded={selected === d.id}
            onClick={() => onSelect(selected === d.id ? "" : d.id)}
          >
            {d.name}
          </button>
          <small className="mono">{targets(d.checks)}</small>
          {d.optional_issues > 0 && (
            <small>{d.optional_issues} opcionales con novedades</small>
          )}
        </td>
        <td>
          {network ? d.pop_name : d.region}
          <small>
            {network
              ? d.device_name || "Sin equipo asociado"
              : [
                  ...new Set(
                    d.checks
                      .map((c: any) => c.catalog_metadata?.city)
                      .filter(Boolean),
                  ),
                ].join(", ")}
          </small>
        </td>
        <td className="num">
          <Metric value={m.p95} unit="ms" level={m.latency} />
        </td>
        <td className="num">
          <Metric value={m.loss} unit="%" level={m.lossLevel} />
        </td>
        <td>
          <div className="check-chips">
            {d.checks.map((c: any) => (
              <span
                className={"badge probe-status " + c.state}
                key={c.id}
                title={(labels[c.state] || c.state) + " · " + c.address}
              >
                {(c.protocol || "icmp").toUpperCase()} ·{" "}
                {c.family === "ipv6" ? "v6" : "v4"}
                {c.essential ? "" : " · opc."}
              </span>
            ))}
          </div>
        </td>
        <td className="num">{d.incidents || "—"}</td>
        <td className="row-actions">
          {admin && (
            <button className="quiet" onClick={() => onConfigure(d)}>
              Organizar
            </button>
          )}
        </td>
      </tr>
    );
  }
  const tableHead = (
    <thead>
      <tr>
        {header("state", "Estado")}
        {header("name", "Destino")}
        <th>{network ? "POP / equipo" : "Región"}</th>
        {header("p95", "Latencia p95", true)}
        {header("loss", "Pérdida / fallos", true)}
        <th>Comprobaciones</th>
        {header("incidents", "Incidentes", true)}
        <th>
          <span className="visually-hidden">Acciones</span>
        </th>
      </tr>
    </thead>
  );
  const providers = [
    ...new Set<string>(visible.map((d: any) => d.provider)),
  ].sort((a, b) => {
    const state = (p: string) =>
      stateOrder[groupState(visible.filter((d: any) => d.provider === p))] ??
      3;
    return state(a) - state(b) || a.localeCompare(b);
  });
  const empty = !visible.length && (
    <p className="muted">No hay destinos para estos filtros.</p>
  );
  return (
    <>
      <section className="panel probe-panel">
        <div className="noc-toolbar">
          <label>
            Buscar{" "}
            {network ? "POP, equipo o destino" : "proveedor, región o destino"}
            <input
              type="search"
              value={query}
              onChange={(e) => reset(() => setQuery(e.target.value))}
              placeholder={network ? "Cúcuta, PE…" : "AWS, São Paulo…"}
            />
          </label>
          {!network && (
            <label>
              Categoría
              <select
                value={category}
                onChange={(e) => reset(() => setCategory(e.target.value))}
              >
                <option value="">Todas las categorías</option>
                {categories.map((c) => (
                  <option key={c} value={c}>
                    {c.replaceAll("_", " ").toUpperCase()}
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>
        <div className="filter-chips" role="group" aria-label="Filtrar por estado">
          {chips.map(([key, label, test]) => (
            <button
              key={key}
              className={"chip chip-" + key}
              aria-pressed={status === key}
              onClick={() => reset(() => setStatus(key))}
            >
              {label}
              <b>{base.filter(test).length}</b>
            </button>
          ))}
        </div>
        {network && (
          <>
            <PopGrid
              data={data}
              selected={parent}
              onSelect={(key) =>
                reset(() => setParent(parent === key ? "" : key))
              }
            />
            {parent && (
              <button
                className="quiet"
                onClick={() => reset(() => setParent(""))}
              >
                Ver todos los POPs
              </button>
            )}
          </>
        )}
        <p className="muted">
          Estado de las comprobaciones esenciales · Origen de medición: servidor
          ODIN
        </p>
      </section>
      {network && (
        <div className="probe-actions" role="group" aria-label="Vista de red">
          <button
            className={tab === "destinations" ? "" : "secondary"}
            onClick={() => setTab("destinations")}
          >
            Equipos y destinos
          </button>
          <button
            className={tab === "links" ? "" : "secondary"}
            onClick={() => setTab("links")}
          >
            Enlaces
          </button>
        </div>
      )}
      {network && tab === "links" ? (
        <section className="panel probe-panel">
          <h2>Enlaces físicos</h2>
          {linkFocus && (
            <button className="quiet" onClick={() => setLinkFocus("")}>
              Ver todos los enlaces
            </button>
          )}
          <p className="muted">
            El estado del enlace utiliza sus comprobaciones asociadas. La
            respuesta de sus equipos extremos se muestra por separado.
          </p>
          <div className="probe-table">
            <table className="noc-table">
              <thead>
                <tr>
                  <th>Enlace</th>
                  <th>Estado del enlace</th>
                  <th>Equipos extremos</th>
                  <th>Puertos</th>
                  <th>VLAN</th>
                  <th>
                    <span className="visually-hidden">Comprobaciones</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {(data?.links || [])
                  .filter((l: any) => !linkFocus || l.id === linkFocus)
                  .filter(
                    (l: any) =>
                      !parent ||
                      (parent === "unassigned"
                        ? !l.pop_ids.length
                        : l.pop_ids.includes(parent)),
                  )
                  .sort(
                    (a: any, b: any) =>
                      (stateOrder[a.state] ?? 3) - (stateOrder[b.state] ?? 3) ||
                      a.name.localeCompare(b.name),
                  )
                  .map((l: any) => (
                    <tr key={l.id} className={"sev-" + l.state}>
                      <td>
                        <strong>{l.name}</strong>
                      </td>
                      <td>
                        <ProbeBadge state={l.state} />
                      </td>
                      <td>
                        <ProbeBadge state={l.endpoint_state} />
                      </td>
                      <td>
                        {l.left_port || "Sin asignar"} ↔{" "}
                        {l.right_port || "Sin asignar"}
                      </td>
                      <td>{l.vlans.join(", ") || "—"}</td>
                      <td className="row-actions">
                        {l.destinations.map((id: string) => (
                          <button
                            className="quiet"
                            key={id}
                            onClick={() => onSelect(id)}
                          >
                            Ver comprobaciones
                          </button>
                        ))}
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
          {!data?.links?.length && (
            <p className="muted">No hay enlaces físicos registrados.</p>
          )}
        </section>
      ) : network ? (
        <section className="panel probe-panel">
          <div className="title-row">
            <h2>Equipos y destinos</h2>
            <small>
              {visible.length} de {scoped.length} destinos
            </small>
          </div>
          <Paged items={visible} focus={(d: any) => d.id === selected}>{(page) => <div className="probe-table">
            <table className="noc-table">
              {tableHead}
              <tbody>{page.map(row)}</tbody>
            </table>
          </div>}</Paged>
          {empty}
          {parent && (
            <details>
              <summary>Equipos del inventario sin sondeo asociado</summary>
              {(data?.devices || [])
                .filter(
                  (d: any) =>
                    (d.pop_id || "unassigned") === parent &&
                    !scoped.some((x: any) => x.device_id === d.id),
                )
                .map((d: any) => (
                  <p key={d.id}>
                    {d.name} <ProbeBadge state="unmonitored" />
                  </p>
                ))}
            </details>
          )}
        </section>
      ) : (
        <section className="panel probe-panel">
          <div className="title-row">
            <h2>Proveedores y regiones</h2>
            <small>
              {visible.length} de {scoped.length} destinos
            </small>
          </div>
          <div className="probe-table">
            <table className="noc-table">
              {tableHead}
              {providers.map((provider) => {
                const members = visible.filter(
                  (d: any) => d.provider === provider,
                );
                const state = groupState(members);
                // Providers with problems, an active filter or the chosen row stay open.
                const open =
                  openGroups[provider] ??
                  (affectedStates.includes(state) ||
                    !!query ||
                    status !== "all" ||
                    members.some((d: any) => d.id === selected));
                return (
                  <tbody key={provider}>
                    <tr className="group-row">
                      <th colSpan={columns}>
                        <button
                          className="group-toggle"
                          aria-expanded={open}
                          onClick={() =>
                            setOpenGroups({ ...openGroups, [provider]: !open })
                          }
                        >
                          <span aria-hidden="true">{open ? "▾" : "▸"}</span>
                          <strong>{provider}</strong>
                          <small>{members.length} destinos</small>
                          <ProviderSummary members={members} />
                          <span className="provider-detail-hint">{open ? "Ocultar destinos" : "Ver destinos"}</span>
                        </button>
                      </th>
                    </tr>
                    {open && members.map(row)}
                  </tbody>
                );
              })}
            </table>
          </div>
          {empty}
        </section>
      )}
      {selectedGroup && (
        <section className="panel probe-panel monitor-selected">
          <div className="title-row">
            <h2>{selectedGroup.name}</h2>
            <ProbeBadge state={selectedGroup.state} />
          </div>
          <p>
            {selectedGroup.check_count} comprobaciones ·{" "}
            {selectedGroup.pop_name} ·{" "}
            {selectedGroup.scope === "external"
              ? selectedGroup.provider
              : "Red propia"}
          </p>
          {admin && (
            <button onClick={() => onAdd(selectedGroup)}>
              Añadir comprobación
            </button>
          )}{" "}
          <button className="quiet" onClick={() => onSelect("")}>
            Cerrar destino
          </button>
        </section>
      )}
    </>
  );
}
export function DestinationOrganization({
  destination,
  data,
  api,
  onSaved,
  onClose,
  onError,
}: {
  destination: any;
  data: any;
  api: Api;
  onSaved: () => Promise<void>;
  onClose: () => void;
  onError: (s: string) => void;
}) {
  const [value, setValue] = useState({ ...destination }),
    [busy, setBusy] = useState(false);
  return (
    <section className="panel probe-panel probe-editor">
      <h2>Organizar destino</h2>
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          try {
            await api("/monitoring/destinations/" + value.id, "PUT", {
              name: value.name,
              scope: value.scope,
              device_id: value.device_id || null,
              link_id: value.link_id || null,
              version: value.version,
            });
            await onSaved();
            onClose();
          } catch (e) {
            onError((e as Error).message);
          } finally {
            setBusy(false);
          }
        }}
      >
        <div className="probe-form-grid">
          <label>
            Nombre del destino
            <input
              required
              maxLength={120}
              value={value.name}
              onChange={(e) => setValue({ ...value, name: e.target.value })}
            />
          </label>
          <label>
            Ámbito
            <select
              value={value.scope}
              onChange={(e) =>
                setValue({
                  ...value,
                  scope: e.target.value,
                  device_id: null,
                  link_id: null,
                })
              }
            >
              <option value="network">Red propia</option>
              <option value="external">Servicios externos</option>
            </select>
          </label>
          {value.scope === "network" && (
            <>
              <AssociationSelect
                label="Equipo"
                options={data?.devices || []}
                value={value.device_id || ""}
                onChange={(id) =>
                  setValue({ ...value, device_id: id || null, link_id: null })
                }
              />
              <AssociationSelect
                label="Enlace"
                options={data?.links || []}
                value={value.link_id || ""}
                onChange={(id) =>
                  setValue({ ...value, link_id: id || null, device_id: null })
                }
              />
            </>
          )}
        </div>
        <div className="probe-actions">
          <button disabled={busy}>Guardar organización</button>
          <button
            type="button"
            className="quiet"
            disabled={busy}
            onClick={onClose}
          >
            Cancelar
          </button>
        </div>
      </form>
    </section>
  );
}
function routeChanges(changes: Record<string, any> | undefined) {
  return Object.entries(changes || {}).map(
    ([service, c]) => `${service}: antes salía por ${c.before}, ahora por ${c.after}`,
  );
}
/** ODIN's reading of where external services fail: our network, one exit, or the service itself. */
export function Verdict({ verdict }: { verdict: any }) {
  if (!verdict || verdict.state === "normal") return null;
  const [first, ...rest] = verdict.findings || [];
  return (
    <div className={"notice verdict " + verdict.state}>
      <strong>¿Nuestro o de ellos? {verdict.title}</strong>
      {verdict.action && <p>{verdict.action}</p>}
      {routeChanges(first?.route_changes).map((t) => (
        <p key={t}>Cambio de ruta · {t}</p>
      ))}
      {rest.length > 0 && (
        <ul>
          {rest.map((f: any) => (
            <li key={f.title}>
              {f.title}
              {routeChanges(f.route_changes).map((t) => (
                <small key={t}> · cambio de ruta: {t}</small>
              ))}
            </li>
          ))}
        </ul>
      )}
      <small>
        Lectura de ODIN: compara los servicios según su salida, desde un solo
        punto de medición. No es una causa confirmada.
      </small>
    </div>
  );
}
export function OperationsOverview({
  monitor,
  onNavigate,
  onOpenPop,
  onOpenIncident,
  onOpenDestination,
  zone,
}: {
  monitor: Monitor;
  onNavigate: (page: string) => void;
  onOpenPop: (popId: string) => void;
  onOpenIncident: (id: string) => void;
  onOpenDestination: (scope: string, id: string) => void;
  zone: string;
}) {
  const data = monitor.data;
  const now = useNow(5000);
  const { age, stale } = freshness(monitor, now);
  const external = (data?.destinations || []).filter(
    (d: any) => d.scope === "external",
  );
  const badExternal = external.filter((d: any) =>
    affectedStates.includes(d.state),
  );
  const events: Record<string, string> = {
    opened: "Incidente abierto",
    recovered: "Incidente recuperado",
    conditions_changed: "Condición modificada",
    acknowledged: "Incidente reconocido",
    observation: "Observación del NOC",
    configuration_changed: "Configuración modificada",
    maintenance_scheduled: "Mantenimiento programado",
    maintenance_cancelled: "Mantenimiento cancelado",
    target_archived: "Comprobación archivada",
  };
  return (
    <>
      {data && stale && (
        <p role="alert" className="error">
          Los datos mostrados son de hace {age == null ? "—" : ago(age)}. ODIN
          no ha podido actualizar el monitoreo; no los tomes como estado actual.
        </p>
      )}
      {data && data.origin.state !== "healthy" && (
        <p role="alert" className="error">
          El sondeador de ODIN no está entregando mediciones. Todas las
          comprobaciones aparecen sin datos hasta que se recupere.
        </p>
      )}
      <section className="panel probe-panel">
        <div className="title-row">
          <h2>Red propia · POPs</h2>
          <button className="quiet" onClick={() => onNavigate("Red propia")}>
            Ver red
          </button>
        </div>
        <PopGrid data={data} onSelect={(key) => onOpenPop(key)} />
      </section>
      <section className="panel probe-panel">
        <div className="title-row">
          <h2>Cola de atención</h2>
          <button className="quiet" onClick={() => onNavigate("Incidentes")}>
            Ver incidentes
          </button>
        </div>
        <AttentionQueue
          data={data}
          zone={zone}
          onOpenIncident={onOpenIncident}
          onOpenDestination={onOpenDestination}
        />
      </section>
      <div className="monitor-overview-grid">
        <section className="panel probe-panel">
          <div className="title-row">
            <h2>Servicios externos afectados</h2>
            <button
              className="quiet"
              onClick={() => onNavigate("Servicios externos")}
            >
              Ver servicios
            </button>
          </div>
          <Verdict verdict={data?.verdict} />
          {[...new Set<string>(badExternal.map((d: any) => d.provider))].map(
            (provider) => (
              <div className="probe-profile compact" key={provider}>
                <span>
                  <strong>{provider}</strong>
                  <small>
                    {
                      badExternal.filter((d: any) => d.provider === provider)
                        .length
                    }{" "}
                    de{" "}
                    {external.filter((d: any) => d.provider === provider).length}{" "}
                    destinos afectados
                  </small>
                </span>
                <ProviderSummary members={external.filter((d: any) => d.provider === provider)} />
              </div>
            ),
          )}
          {data && !badExternal.length && (
            <p className="muted">
              Sin degradaciones en {external.length} destinos externos.
            </p>
          )}
        </section>
        <section className="panel probe-panel">
          <h2>Salud del monitoreo</h2>
          <div className="probe-profile compact">
            <span>Sondeador ODIN</span>
            <ProbeBadge state={data?.origin.state || "no_data"} />
          </div>
          {[
            ["no_data", "Sin datos"],
            ["pending_validation", "Pendientes de validación"],
            ["warming", "Reuniendo muestras"],
            ["paused", "Pausadas"],
            ["maintenance", "En mantenimiento"],
          ].map(([key, label]) => (
            <div className="probe-profile compact" key={key}>
              <span>{label}</span>
              <strong className="metric">
                {data ? data.health[key] || 0 : "—"}
              </strong>
            </div>
          ))}
          <small>
            {data
              ? `${data.counts.enabled_checks} comprobaciones activas en ${data.counts.destinations} destinos`
              : ""}
          </small>
        </section>
      </div>
      <section className="panel probe-panel">
        <h2>Cambios recientes del monitoreo</h2>
        {data?.recent_changes.length ? (
          <div className="probe-table">
            <table className="noc-table">
              <tbody>
                {data.recent_changes.map((e: any) => (
                  <tr key={e.id}>
                    <td className="time">
                      {new Date(e.at * 1000).toLocaleString("es", {
                        timeZone: zone,
                        dateStyle: "short",
                        timeStyle: "medium",
                      })}
                    </td>
                    <td>{events[e.kind] || "Actualización de incidente"}</td>
                    <td>
                      {e.target_name}
                      <small className="mono">{e.address}</small>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          data && <p className="muted">Sin cambios registrados.</p>
        )}
      </section>
    </>
  );
}

function AssociationSelect({
  label,
  options,
  value,
  onChange,
}: {
  label: string;
  options: any[];
  value: string;
  onChange: (id: string) => void;
}) {
  const id = useId(),
    input = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false),
    [query, setQuery] = useState(""),
    [active, setActive] = useState(0);
  const choices = [{ id: "", name: "Sin asociación" }, ...options].filter((x) =>
    x.name.toLowerCase().includes(query.toLowerCase()),
  );
  function choose(key: string) {
    onChange(key);
    setOpen(false);
    setQuery("");
    input.current?.focus();
  }
  return (
    <div
      className="device-select"
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node)) {
          setOpen(false);
          setQuery("");
        }
      }}
    >
      <label htmlFor={id}>{label}</label>
      <div className="device-combobox">
        <input
          ref={input}
          id={id}
          role="combobox"
          aria-expanded={open}
          aria-controls={id + "-list"}
          aria-autocomplete="list"
          aria-activedescendant={
            open && choices[active] ? id + "-" + active : undefined
          }
          autoComplete="off"
          placeholder="Seleccionar o buscar…"
          value={open ? query : options.find((x) => x.id === value)?.name || ""}
          onClick={() => {
            setOpen(true);
            setQuery("");
            setActive(0);
          }}
          onChange={(e) => {
            setOpen(true);
            setQuery(e.target.value);
            setActive(0);
          }}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              e.preventDefault();
              setOpen(false);
            }
            if (e.key === "ArrowDown" || e.key === "ArrowUp") {
              e.preventDefault();
              setOpen(true);
              setActive(
                Math.max(
                  0,
                  Math.min(
                    choices.length - 1,
                    active + (e.key === "ArrowDown" ? 1 : -1),
                  ),
                ),
              );
            }
            if (e.key === "Enter" && open) {
              e.preventDefault();
              if (choices[active]) choose(choices[active].id);
            }
          }}
        />
        <button
          type="button"
          className="device-toggle"
          aria-label={"Abrir " + label.toLowerCase()}
          onClick={() => {
            setOpen(!open);
            setQuery("");
            setActive(0);
            input.current?.focus();
          }}
        >
          ▾
        </button>
      </div>
      {open && (
        <div className="device-dropdown">
          <div role="listbox" id={id + "-list"}>
            {choices.map((x, index) => (
              <button
                type="button"
                role="option"
                tabIndex={-1}
                id={id + "-" + index}
                key={x.id}
                aria-selected={x.id === value}
                className={index === active ? "active" : ""}
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => choose(x.id)}
              >
                {x.name}
              </button>
            ))}
          </div>
          {!choices.length && <small>Sin resultados</small>}
        </div>
      )}
    </div>
  );
}
