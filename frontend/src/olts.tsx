import { useEffect, useState } from "react";
import { Drawer } from "./drawer";
type Api = (path: string, method?: string, body?: unknown) => Promise<any>;
type Props = { api: Api; zone: string; admin: boolean; onError: (s: string) => void };
const causes: Record<string, string> = {
  los: "Corte de fibra (LOS)",
  power: "Falla de energía",
  offline: "Sin conexión",
  stale: "Caída de más de 7 días",
};
const tasks: Record<string, string> = { inventory: "Inventario", outages: "Caídas de PON", onus: "ONUs" };
const errors: Record<string, string> = {
  rate_limited: "SmartOLT pidió esperar (límite de llamadas)",
  access_denied: "SmartOLT rechazó la API key",
  connection_failed: "No se pudo conectar con SmartOLT",
  timeout: "SmartOLT no respondió a tiempo",
  server_unavailable: "SmartOLT no está disponible",
  smartolt_error: "SmartOLT devolvió un error",
  network_not_allowed: "La dirección de SmartOLT no es pública",
  invalid_response: "Respuesta de SmartOLT no válida",
  too_many_onus: "Demasiadas ONUs para leer",
};
const fmt = (n: number | null | undefined, d = 1) =>
  n == null ? "—" : n.toLocaleString("es", { maximumFractionDigits: d });
const when = (s: number | null | undefined, zone: string) =>
  s ? new Date(s * 1000).toLocaleString("es", { timeZone: zone, dateStyle: "short", timeStyle: "short" }) : "—";
function span(seconds: number | null | undefined) {
  if (seconds == null) return "—";
  const days = Math.floor(seconds / 86400), hours = Math.floor((seconds % 86400) / 3600);
  return days ? `${days} d ${hours} h` : `${hours} h ${Math.floor((seconds % 3600) / 60)} min`;
}
/** The state of an OLT, from the worst of what it reports. */
function oltState(o: any) {
  if (!o.present) return ["no_data", "No reportada"];
  if (!o.reachable) return ["critical", "Inalcanzable"];
  if (o.uplinks_down || o.outages) return ["critical", o.outages ? "Caída de PON" : "Uplink caído"];
  if (o.pons_down) return ["warning", "PON caído"];
  return ["healthy", "Normal"];
}
const Badge = ({ state, text }: { state: string; text: string }) => (
  <span className={"badge probe-status " + state}>{text}</span>
);

function Source({ api, admin, data, onError, reload }: Props & { data: any; reload: () => void }) {
  const [timezone, setTimezone] = useState(data.timezone || "");
  const [busy, setBusy] = useState(false);
  async function act(fn: () => Promise<void>) {
    setBusy(true);
    onError("");
    try {
      await fn();
      reload();
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  if (!data.configured)
    return (
      <p className="notice">
        SmartOLT no está configurado. {admin ? "Agrega el subdominio y la API key en Administración → Fuentes." : "Pide a un administrador que lo configure."}
      </p>
    );
  const budget = data.rate_limit ? `${data.rate_remaining} de ${data.rate_limit} llamadas disponibles en esta hora` : "Presupuesto aún sin observar";
  return (
    <section className="panel">
      <div className="title-row">
        <h2>Lectura de SmartOLT</h2>
        <Badge state={data.enabled ? "healthy" : "paused"} text={data.enabled ? "Activa" : "Desactivada"} />
      </div>
      <p className="muted">
        Caídas de PON cada 5 minutos, inventario cada hora y ONUs una vez al día, con un retraso aleatorio. {budget}.
        {data.blocked_until && data.blocked_until > Date.now() / 1000 && " SmartOLT pidió esperar: lecturas en pausa."}
      </p>
      <div className="probe-table">
        <table>
          <thead>
            <tr><th>Lectura</th><th>Última correcta</th><th>Próxima</th><th>Estado</th></tr>
          </thead>
          <tbody>
            {Object.keys(tasks).map((t) => {
              const info = data.tasks?.[t] || {};
              return (
                <tr key={t}>
                  <td>{tasks[t]}</td>
                  <td>{when(info.last_success_at, "UTC")} UTC</td>
                  <td>{data.due?.[t] ? when(data.due[t], "UTC") + " UTC" : "Ahora"}</td>
                  <td>{info.last_error ? <Badge state="failed" text={errors[info.last_error] || info.last_error} /> : info.last_success_at ? <Badge state="healthy" text="Correcta" /> : "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {admin && (
        <div className="probe-actions">
          <label>
            Zona horaria de la cuenta SmartOLT
            <input value={timezone} placeholder="America/Bogota (vacío: UTC)" onChange={(e) => setTimezone(e.target.value)} />
          </label>
          <button disabled={busy} onClick={() => void act(() => api("/smartolt/settings", "PUT", { version: data.version, enabled: !data.enabled, timezone: timezone || null }))}>
            {data.enabled ? "Desactivar lectura" : "Activar lectura"}
          </button>
          {data.enabled && (
            <button className="secondary" disabled={busy} onClick={() => void act(() => api("/smartolt/settings", "PUT", { version: data.version, enabled: true, timezone: timezone || null }))}>
              Guardar zona
            </button>
          )}
          {data.enabled && (
            <button className="secondary" disabled={busy} onClick={() => void act(() => api("/smartolt/refresh", "POST"))}>
              Actualizar ahora
            </button>
          )}
        </div>
      )}
    </section>
  );
}

function Detail({ api, admin, zone, onError, id, onClose }: Props & { id: string; onClose: () => void }) {
  const [data, setData] = useState<any>(null), [pops, setPops] = useState<any[]>([]);
  const load = () => api("/olts/" + id).then(setData).catch((e) => onError((e as Error).message));
  useEffect(() => {
    void load();
    if (admin) api("/pops").then(setPops).catch(() => {});
    const timer = setInterval(() => void load(), 30000);
    return () => clearInterval(timer);
  }, [id]);
  if (!data) return <section className="panel"><p className="muted">Cargando OLT…</p></section>;
  const [state, label] = oltState(data);
  return (
    <section className="panel probe-panel">
      <div className="title-row">
        <div>
          <h2>{data.name} <Badge state={state} text={label} /></h2>
          <small><span className="mono">{data.ip || "Sin IP"}</span> · {data.hardware || "Modelo desconocido"} · uptime {span(data.uptime_seconds)} · {fmt(data.temperature)} °C</small>
        </div>
        <button className="quiet" onClick={onClose}>Cerrar</button>
      </div>
      <p className="muted">
        {data.device_name ? `Equipo del inventario: ${data.device_name}` : "Sin equipo del inventario con su IP"} · POP: {data.pop_name || "sin asignar"}
        {data.pop_manual ? " (asignado a mano)" : ""}
      </p>
      {admin && (
        <label>
          POP
          <select value={data.pop_manual ? data.pop_id : ""} onChange={(e) => api("/olts/" + id, "PUT", { pop_id: e.target.value || null }).then(load).catch((x) => onError((x as Error).message))}>
            <option value="">El del equipo del inventario</option>
            {pops.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        </label>
      )}
      <h3>Caídas activas</h3>
      {data.active_outages.length === 0 ? <p className="muted">Sin caídas de PON.</p> : (
        <div className="probe-table">
          <table>
            <thead><tr><th>PON</th><th>Causa</th><th>Afectadas</th><th>Zona</th><th>Splitters</th><th>Desde</th></tr></thead>
            <tbody>
              {data.active_outages.map((o: any) => (
                <tr key={o.id} className={o.cause === "stale" ? "" : "olt-outage"}>
                  <td><span className="mono">{o.board}/{o.port}</span><small>{o.description}</small></td>
                  <td><Badge state={o.cause === "stale" ? "no_data" : o.cause === "los" ? "critical" : "warning"} text={causes[o.cause] || o.cause} /></td>
                  <td>{o.affected} de {o.subscribers} ONUs</td>
                  <td>{o.zone || "—"}</td>
                  <td>{o.odbs.length ? <>{o.odbs.length}<small>{o.odbs.slice(0, 6).join(", ")}{o.odbs.length > 6 ? "…" : ""}</small></> : "—"}</td>
                  <td>{when(o.started_at || o.first_seen, zone)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <h3>Uplinks</h3>
      <div className="probe-table">
        <table>
          <thead><tr><th>Puerto</th><th>Estado</th><th>Negociación</th><th>Temperatura</th><th>Descripción</th></tr></thead>
          <tbody>
            {data.uplink_ports.map((u: any) => (
              <tr key={u.name}>
                <td className="mono">{u.name}</td>
                <td><Badge state={u.down ? "critical" : u.admin_status === "Enabled" ? "healthy" : "paused"} text={u.status || u.admin_status} /></td>
                <td>{u.negotiation || "—"}</td>
                <td>{u.temperature == null ? "—" : fmt(u.temperature) + " °C"}</td>
                <td>{u.description || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <h3>Puertos PON</h3>
      <div className="probe-table">
        <table>
          <thead><tr><th>PON</th><th>Estado</th><th className="num">ONUs en línea</th><th className="num">Señal media</th><th className="num">TX</th><th>Señal de ONUs</th></tr></thead>
          <tbody>
            {data.pon_ports.map((p: any) => (
              <tr key={p.board + "/" + p.port}>
                <td><span className="mono">{p.board}/{p.port}</span><small>{p.pon_type}{p.description ? " · " + p.description : ""}</small></td>
                <td>
                  <Badge state={p.outage ? "critical" : p.down ? "warning" : p.admin_status === "Enabled" ? "healthy" : "paused"} text={p.outage ? causes[p.outage] : p.status || p.admin_status} />
                </td>
                <td className="num">{p.online} / {p.onus}</td>
                <td className="num">{p.average_signal == null ? "—" : fmt(p.average_signal, 2) + " dBm"}</td>
                <td className="num">{p.tx_power == null ? "—" : fmt(p.tx_power, 2) + " dBm"}</td>
                <td>{p.critical ? <span className="olt-critical">{p.critical} críticas</span> : null}{p.critical && p.warning ? " · " : ""}{p.warning ? `${p.warning} en advertencia` : ""}{!p.critical && !p.warning ? "—" : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted">La señal de cada ONU se lee una vez al día; los estados en tiempo real llegarán con los eventos de SmartOLT.</p>
      {data.ended_outages.length > 0 && (
        <details>
          <summary>Caídas terminadas</summary>
          <div className="probe-table">
            <table>
              <thead><tr><th>PON</th><th>Causa</th><th>Afectadas</th><th>Desde</th><th>Hasta</th></tr></thead>
              <tbody>
                {data.ended_outages.map((o: any) => (
                  <tr key={o.id}>
                    <td className="mono">{o.board}/{o.port}</td><td>{causes[o.cause] || o.cause}</td><td>{o.affected} de {o.subscribers}</td>
                    <td>{when(o.started_at || o.first_seen, zone)}</td><td>{when(o.ended_at, zone)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
    </section>
  );
}

export function Olts(props: Props & { focusId?: string }) {
  const { api, onError } = props;
  const [data, setData] = useState<any>(null), [selected, setSelected] = useState<string | null>(props.focusId || null);
  const load = () => api("/olts").then(setData).catch((e) => onError((e as Error).message));
  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), 30000);
    return () => clearInterval(timer);
  }, []);
  if (!data) return <p className="muted">Cargando OLTs…</p>;
  return (
    <>
      <h1>OLTs</h1>
      <Source {...props} data={data.source} reload={load} key={data.source.version} />
      <section className="panel">
        <div className="title-row">
          <h2>OLTs</h2>
          <small>{data.olts.length} OLT</small>
        </div>
        {data.olts.length === 0 ? (
          <p className="muted">Sin OLTs todavía: aparecen con la primera lectura del inventario.</p>
        ) : (
          <div className="probe-table">
            <table>
              <thead>
                <tr><th>Estado</th><th>OLT</th><th>POP / equipo</th><th className="num">ONUs en línea</th><th className="num">PON caídos</th><th className="num">Caídas</th><th>Uptime</th><th className="num">Temp.</th></tr>
              </thead>
              <tbody>
                {data.olts.map((o: any) => {
                  const [state, label] = oltState(o);
                  return (
                    <tr key={o.id} className={"sev-" + state}>
                      <td><Badge state={state} text={label} /></td>
                      <td>
                        <button className="link" onClick={() => setSelected(o.id)}>{o.name}</button>
                        <small><span className="mono">{o.ip || "Sin IP"}</span> · {o.hardware}</small>
                      </td>
                      <td>{o.pop_name || "Sin POP"}<small>{o.device_name || "Sin equipo asociado"}</small></td>
                      <td className="num">{o.online} / {o.onus}</td>
                      <td className="num">{o.pons_down} / {o.pons}</td>
                      <td className="num">{o.outages ? <>{o.outages}<small>{o.affected} ONUs</small></> : "—"}</td>
                      <td>{span(o.uptime_seconds)}</td>
                      <td className="num">{o.temperature == null ? "—" : fmt(o.temperature) + " °C"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
      {selected && (
        <Drawer label={"OLT · " + (data.olts.find((o: any) => o.id === selected)?.name || "")} link={"#olt=" + selected} onClose={() => setSelected(null)}>
          <Detail {...props} id={selected} onClose={() => setSelected(null)} />
        </Drawer>
      )}
    </>
  );
}
