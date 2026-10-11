import { useEffect, useState } from "react";
import { Paged } from "./pager";
type Api = (path: string, method?: string, body?: unknown) => Promise<any>;
const reasons: Record<string, string> = {
  baseline: "Línea base",
  incident_opened: "Apertura de incidente",
  incident_recovered: "Recuperación de incidente",
  manual: "Manual",
};
const states: Record<string, string> = {
  pending: "Pendiente",
  running: "En curso",
  done: "Completada",
  failed: "Fallida",
  cancelled: "Cancelada",
};
const errors: Record<string, string> = {
  trace_timeout: "La traza no terminó a tiempo",
  trace_execution_failed: "Error local al ejecutar mtr",
  trace_parse_failed: "Respuesta de mtr no válida",
  trace_interrupted: "Ejecución interrumpida",
  trace_skipped: "Omitida: llegó tarde y la reemplaza la siguiente",
  trace_address_changed: "La comprobación ya no mide esa dirección",
  configuration_changed: "La comprobación cambió o se archivó",
  mtr_unavailable: "Trazador no disponible",
};
const roles: Record<string, string> = {
  transit: "tránsito",
  transport: "transporte",
  ixp: "IXP",
  peering: "peering",
  customer: "cliente",
  cdn: "CDN interno",
};
const fmt = (n: number | null | undefined, d = 1) =>
  n == null ? "—" : n.toLocaleString("es", { maximumFractionDigits: d });
const when = (s: number | null, zone: string) =>
  s ? new Date(s * 1000).toLocaleString("es", { timeZone: zone }) : "—";
function handoff(h: any) {
  if (!h) return "";
  const role = h.role ? roles[h.role] || h.role : "enlace sin rol";
  return `${role}${h.provider && h.role !== "transport" ? " " + h.provider : ""} · ${h.device_name} ${h.interface}`;
}
function where(h: any) {
  switch (h.segment) {
    case "own":
      return `Red propia · ${h.device_name}${h.interface ? " " + h.interface : ""}`;
    case "handoff":
      return "Frontera · " + handoff(h);
    case "destination":
      return h.device_name ? `Destino · ${h.device_name}` : "Destino";
    case "external":
      return "Externa";
    case "private":
      return "Privada, fuera del inventario";
    default:
      return "Sin respuesta";
  }
}
function PathSummary({ trace }: { trace: any }) {
  const p = trace.path || {};
  if (trace.status !== "done") return null;
  return (
    <small>
      {trace.reached ? "Llegó al destino" : "No llegó al destino"} ·{" "}
      {trace.hop_count} saltos
      {p.handoff && <> · sale por {handoff(p.handoff)}</>}
      {!p.handoff && p.last_own && <> · último equipo propio {p.last_own.device_name}</>}
      {p.asns?.length > 0 && <> · AS {p.asns.join(" → ")}</>}
    </small>
  );
}
function Hops({ hops }: { hops: any[] }) {
  return (
    <div className="probe-table">
      <table className="trace-hops">
        <thead>
          <tr>
            <th>#</th>
            <th>Dirección</th>
            <th>Ubicación</th>
            <th>Pérdida</th>
            <th>Media · mejor · peor (ms)</th>
          </tr>
        </thead>
        <tbody>
          {hops.map((h) => (
            <tr key={h.hop} className={"trace-" + h.segment}>
              <td>{h.hop}</td>
              <td>
                {h.address || "—"}
                {h.asn && <small>AS{h.asn}</small>}
              </td>
              <td>{where(h)}</td>
              <td>{h.address ? fmt(h.loss) + " %" : "—"}</td>
              <td>
                {h.avg == null
                  ? "—"
                  : `${fmt(h.avg, 2)} · ${fmt(h.best, 2)} · ${fmt(h.worst, 2)}`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
/** Traces of a check (with `targetId`, polled and with a manual trace) or a given list, e.g. those of an incident. */
export function Traces({
  api,
  zone,
  onError,
  targetId,
  items,
}: {
  api: Api;
  zone: string;
  onError: (s: string) => void;
  targetId?: string;
  items?: any[];
}) {
  const [loaded, setLoaded] = useState<any[]>([]),
    [selected, setSelected] = useState<string | null>(null),
    [detail, setDetail] = useState<any>(null),
    [busy, setBusy] = useState(false);
  const traces = items || loaded;
  const active = traces.some((t) => t.status === "pending" || t.status === "running");
  async function load() {
    if (targetId) setLoaded(await api(`/targets/${targetId}/traces`));
  }
  useEffect(() => {
    setSelected(null);
    let live = true;
    const run = () => {
      if (live) load().catch((e) => onError((e as Error).message));
    };
    run();
    // Faster while a trace is running: it takes about ten seconds.
    const timer = setInterval(run, active ? 4000 : 15000);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [targetId, active]);
  const current = traces.find((t) => t.id === selected);
  useEffect(() => {
    if (!selected) return setDetail(null);
    api("/traces/" + selected)
      .then(setDetail)
      .catch((e) => onError((e as Error).message));
  }, [selected, current?.status]);
  return (
    <details open={!targetId || undefined}>
      <summary>Trazas de ruta</summary>
      <p className="muted">
        Ruta desde el servidor ODIN (mtr, 10 ciclos): línea base cada 15
        minutos, al abrirse y al recuperarse un incidente. No muestra la ruta de
        regreso. Un salto sin respuesta o con pérdida que no continúa hasta el
        destino suele ser un router que limita ICMP, no pérdida real. El núcleo
        MPLS de un proveedor puede aparecer como menos saltos.
      </p>
      {targetId && (
        <button
          className="secondary"
          disabled={busy || active}
          onClick={async () => {
            setBusy(true);
            onError("");
            try {
              const created = await api(`/targets/${targetId}/traces`, "POST");
              await load();
              setSelected(created.id);
            } catch (e) {
              onError((e as Error).message);
            } finally {
              setBusy(false);
            }
          }}
        >
          {active ? "Trazando…" : "Trazar ahora"}
        </button>
      )}
      {traces.length === 0 ? (
        <p className="muted">Sin trazas todavía.</p>
      ) : (
        <Paged items={traces} size={10} focus={(t) => t.id === selected}>
          {(page) => (
            <div className="probe-table">
              <table>
                <thead>
                  <tr>
                    <th>Hora</th>
                    <th>Motivo</th>
                    <th>Resultado</th>
                  </tr>
                </thead>
                <tbody>
                  {page.map((t) => (
                    <tr
                      key={t.id}
                      className={"trace-row" + (t.id === selected ? " selected" : "")}
                      onClick={() => setSelected(t.id === selected ? null : t.id)}
                    >
                      <td>
                        {when(t.finished_at || t.slot, zone)}
                        <small>
                          {t.address} · {t.protocol === "tcp" ? `TCP ${t.port}` : "ICMP"}
                        </small>
                      </td>
                      <td>{reasons[t.reason] || t.reason}</td>
                      <td>
                        <span className={"badge probe-status " + t.status}>
                          {states[t.status] || t.status}
                        </span>
                        {t.path_changed && (
                          <span className="badge probe-status warning">Cambio de ruta</span>
                        )}
                        {t.error_code && <small>{errors[t.error_code] || t.error_code}</small>}
                        <PathSummary trace={t} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Paged>
      )}
      {detail?.hops?.length > 0 && (
        <>
          <h3>
            Saltos · {when(detail.finished_at, zone)} · {reasons[detail.reason] || detail.reason}
          </h3>
          <Hops hops={detail.hops} />
        </>
      )}
    </details>
  );
}
