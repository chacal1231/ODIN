import { useEffect, useState } from "react";
import { Paged } from "./pager";
import { AssociationPicker } from "./associations";

type Api = (path: string, method?: string, body?: unknown) => Promise<any>;
const sources: Record<string, string> = {
  zabbix: "Zabbix", librenms: "LibreNMS", graylog: "Graylog",
};
const coverage: Record<string, string> = {
  complete: "Completa", partial: "Parcial", unavailable: "No disponible", unsupported: "No compatible",
};
const kinds: Record<string, string> = {
  log_bgp: "Log BGP", log_interface: "Log de interfaz", log_bfd: "Log BFD", log_config: "Cambio de configuración",
  log_system: "Log del sistema", log_other: "Log",
};
const states: Record<string, string> = { open: "Abierto", recovered: "Recuperado", info: "Informativo", withdrawn: "Retirado en origen" };
const severities: Record<string, string> = { info: "Informativa", warning: "Advertencia", critical: "Crítica", unknown: "Desconocida" };
export const reasons: Record<string, string> = {
  pagination: "Faltan páginas por consultar", truncated: "Consulta limitada", timeout: "Tiempo de espera agotado",
  unavailable: "Fuente no disponible", unsupported: "Lectura no compatible", history_unavailable: "Sin historial disponible",
  snapshot_only: "Estado actual; no acredita cambios entre consultas", retained_history: "Historial disponible en origen; sujeto a su retención",
  connection_failed: "No se pudo conectar", access_denied: "La fuente rechazó el acceso", rpc_access_or_parameters: "Revisa los permisos de la API",
  timezone_required: "Falta definir la zona horaria", source_not_configured: "Faltan credenciales o redes permitidas",
  checkpoint_conflict: "Conflicto de revisión; requiere revisar el origen", checkpoint_stale: "Otra lectura avanzó el flujo; se reintenta",
  revision_conflict: "El origen devolvió dos versiones distintas de un evento con la misma fecha", identity_changed: "El origen cambió la identidad de un evento",
  time_basis_changed: "Cambió la base temporal de un flujo", page_key_reused: "Página repetida con contenido distinto", event_not_visible: "Un evento anterior ya no es visible; no se presume recuperado",
  recovery_unavailable: "No se pudo verificar una recuperación", attempts_exhausted: "Se agotaron los intentos del trabajo",
  snapshot_limit: "La lista de alertas supera el límite de 500", invalid_response: "La fuente devolvió datos incompatibles",
  invalid_pagination: "La paginación no respeta el contrato esperado", bootstrap: "Localizando el log más antiguo disponible",
  window_split: "Ventana dividida por volumen; se lee por partes", invalid_source_time: "La fuente devolvió una fecha no reconocida", incomplete_history_page: "La página del historial llegó incompleta",
};
export function SourceEvents({ api, zone, admin }: { api: Api; zone: string; admin: boolean }) {
  const [source, setSource] = useState("");
  const [status, setStatus] = useState("");
  const [hours, setHours] = useState("24");
  const [cursors, setCursors] = useState<string[]>([""]);
  const [data, setData] = useState<any>(null);
  const [sourceSettings, setSourceSettings] = useState<any[]>([]);
  const [busy, setBusy] = useState(false);
  const [timezones, setTimezones] = useState<Record<string, string>>({});
  const [streams, setStreams] = useState<any[]>([]);
  const [selected, setSelected] = useState<any>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const [updated, setUpdated] = useState<number | null>(null);
  const cursor = cursors[cursors.length - 1];
  const date = (stamp: number | null) => stamp == null ? "—" : new Date(stamp * 1000).toLocaleString("es", { timeZone: zone });
  useEffect(() => {
    let active = true;
    const load = async () => {
      setLoading(true);
      const query = new URLSearchParams({ limit: "50" });
      if (source) query.set("source", source);
      if (status) query.set("status", status);
      if (hours) query.set("start", String(Math.floor(Date.now() / 1000) - Number(hours) * 3600));
      if (cursor) query.set("cursor", cursor);
      try {
        const [events, coverageData, settings] = await Promise.all([api("/source-events?" + query), api("/event-streams"), api("/event-sources")]);
        if (active) { setData(events); setSourceSettings(settings); setStreams(coverageData.streams); setUpdated(coverageData.observed_at); setError(""); }
      } catch (e) { if (active) setError((e as Error).message); }
      finally { if (active) setLoading(false); }
    };
    setData(null); setSelected(null); void load();
    const timer = window.setInterval(load, 30000);
    return () => { active = false; window.clearInterval(timer); };
  }, [api, source, status, hours, cursor, refresh]);
  const filter = (set: (v: string) => void, value: string) => { set(value); setCursors([""]); };
  const configure = async (row: any, enabled: boolean) => {
    setBusy(true);
    try {
      await api("/event-sources/" + row.source, "PUT", { version: row.version, enabled, timezone: timezones[row.source] ?? row.timezone });
      setRefresh((n) => n + 1);
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };
  return <>
    <section className="panel probe-panel">
      <div className="title-row"><h2>Eventos de fuentes</h2><button className="quiet" disabled={loading} onClick={() => { setCursors([""]); setRefresh(refresh + 1); }}>Actualizar</button></div>
      <p>Eventos recibidos de las fuentes externas. Sus estados pertenecen a la fuente; la correlación está en Casos.</p>
      <div className="event-filters">
        <label>Fuente<select value={source} onChange={(e) => filter(setSource, e.target.value)}><option value="">Todas</option>{Object.entries(sources).map(([key, name]) => <option key={key} value={key}>{name}</option>)}</select></label>
        <label>Estado<select value={status} onChange={(e) => filter(setStatus, e.target.value)}><option value="">Todos</option><option value="changes">Solo cambios de estado</option>{Object.entries(states).map(([key, name]) => <option key={key} value={key}>{name}</option>)}</select></label>
        <label>Período<select value={hours} onChange={(e) => filter(setHours, e.target.value)}><option value="24">Últimas 24 horas</option><option value="168">Últimos 7 días</option><option value="720">Últimos 30 días</option><option value="">Todo el historial</option></select></label>
      </div>
      {error && <p role="alert">No se pudo actualizar: {error}. Los datos anteriores pueden estar desactualizados.</p>}
      <p className="muted">{loading ? "Consultando…" : `Última consulta: ${date(updated)}`}</p>
      {data && !data.items.length && <p>No hay eventos recibidos para estos filtros. Esto no confirma que las fuentes estén libres de problemas; revisa la cobertura.</p>}
      {!!data?.items.length && <div className="probe-table"><table>
        <thead><tr><th>Fecha de origen</th><th>Fuente</th><th>Evento</th><th>Severidad</th><th>Estado</th></tr></thead>
        <tbody>{data.items.map((event: any) => <tr key={event.id}>
          <td>{date(event.occurred_at)}</td><td>{sources[event.source]}</td>
          <td><button className="link" aria-expanded={selected?.id === event.id} onClick={() => setSelected(selected?.id === event.id ? null : event)}>{event.title}</button></td>
          <td>{severities[event.severity]}</td><td>{event.present_in_latest_snapshot === false ? "No presente en la última lectura" : event.kind === "librenms_history" ? (event.status === "recovered" ? "Recuperación registrada" : "Alerta registrada") : states[event.status]}</td>
        </tr>)}</tbody>
      </table></div>}
      <div className="event-filters">
        <button className="quiet" disabled={loading || cursors.length === 1} onClick={() => setCursors(cursors.slice(0, -1))}>Anterior</button>
        <span>Página {cursors.length}</span>
        <button className="quiet" disabled={loading || !data?.next_cursor} onClick={() => setCursors([...cursors, data.next_cursor])}>Siguiente</button>
      </div>
    </section>
    {selected && <section className="panel probe-panel">
      <div className="title-row"><h2>{selected.title}</h2><button className="quiet" onClick={() => setSelected(null)}>Cerrar detalle</button></div>
      <dl className="event-detail">
        <dt>Fuente / flujo</dt><dd>{sources[selected.source]} / {selected.stream}</dd>
        <dt>Referencia de origen</dt><dd>{selected.reference}</dd>
        <dt>ID externo</dt><dd>{selected.external_id}</dd>
        <dt>Entidad de origen</dt><dd>{selected.entity_id || "Sin entidad informada"}</dd>
        <dt>Inventario</dt><dd>{selected.device_id ? selected.device_name || "Equipo asociado" : "Sin asociación confirmada"}</dd>
        <dt>Estado original</dt><dd>{selected.original_state || "No informado"}</dd>
        <dt>Severidad original</dt><dd>{selected.original_severity || "No informada"}</dd>
        <dt>Tipo</dt><dd>{kinds[selected.kind] || selected.kind}</dd>
        <dt>Fecha de origen</dt><dd>{date(selected.occurred_at)}</dd>
        <dt>Actualización en origen</dt><dd>{date(selected.source_updated_at)}</dd>
        <dt>Primera recepción</dt><dd>{date(selected.received_at)}</dd>
        <dt>Última recepción</dt><dd>{date(selected.last_received_at)}</dd>
        {selected.recovery_of && <><dt>Recupera el evento</dt><dd>{selected.recovery_of} (misma fuente y flujo)</dd></>}
      </dl>
      {selected.present_in_latest_snapshot === false && <p>La alerta no apareció en la última lectura. Se conserva su último estado conocido; su ausencia no acredita recuperación.</p>}
      {selected.time_basis === "snapshot" && <p>Estado observado en la última consulta. La fecha en origen no necesariamente refleja cada reconocimiento o cambio; el historial se consulta por separado.</p>}
      {selected.kind === "librenms_history" && <p>Transición histórica: no representa por sí sola el estado actual de esta alerta. La severidad se conserva solo si figura en el registro histórico.</p>}
      {selected.source === "graylog" && <p>Fecha de recepción en Graylog. La hora que informa el equipo no se usa: varios PE envían su hora local como UTC o tienen el reloj desfasado.</p>}
      {selected.identity_kind === "fingerprint" && <p>Identidad calculada: observaciones idénticas pueden ser indistinguibles en esta fuente.</p>}
    </section>}
    <section className="panel probe-panel">
      <h2>Lectura automática de fuentes</h2>
      {sourceSettings.filter((row) => !source || row.source === source).map((row) => <div className="probe-profile" key={row.source}>
        <strong>{sources[row.source]} · {!row.configured ? "Sin configurar" : row.enabled ? "Activada" : "Pausada"}</strong>
        <p>Intervalo previsto: {row.interval_seconds} segundos. {row.source === "librenms" && `Zona horaria: ${row.timezone || "Sin definir"}.`}</p>
        {row.collectors.map((c: any) => <p key={c.topic}>
          {c.topic === "events" ? "Eventos y problemas" : c.topic === "alerts" ? "Estado actual de alertas" : c.topic === "logs" ? "Logs" : "Historial de alertas"}: {c.last_error ? (reasons[c.last_error] || "Error de lectura") : !c.last_success_at ? "Esperando primera lectura" : c.stale ? "Lectura desactualizada" : "Lectura reciente"}
          <small>Último intento: {date(c.last_attempt_at)} · Último éxito: {date(c.last_success_at)} · Próxima lectura prevista: {date(c.next_due)}</small>
        </p>)}
        {admin && row.configured && <div className="event-filters">
          {row.source === "librenms" && <label>Zona horaria del servidor<input value={timezones[row.source] ?? row.timezone ?? ""} placeholder="America/Bogota" onChange={(e) => setTimezones({ ...timezones, [row.source]: e.target.value })} /></label>}
          <button disabled={busy} className="quiet" onClick={() => void configure(row, !row.enabled)}>{row.enabled ? "Pausar lectura" : "Activar lectura"}</button>
          {row.source === "librenms" && <button disabled={busy} className="quiet" onClick={() => void configure(row, row.enabled)}>Guardar zona horaria</button>}
          {row.enabled && <button disabled={busy} className="quiet" onClick={async () => {
            setBusy(true);
            try { await api("/event-sources/" + row.source + "/retry", "POST"); setRefresh((n) => n + 1); }
            catch (e) { setError((e as Error).message); } finally { setBusy(false); }
          }}>Solicitar lectura</button>}
        </div>}
      </div>)}
      <h2>Cobertura de ingestión</h2>
      <p>La cobertura corresponde a la ventana indicada de la última consulta. Comprueba también su fecha: una lectura completa antigua no garantiza datos actuales.</p>
      {!streams.length && <p>Aún no hay lecturas de eventos registradas. Revisa la activación y el estado de las fuentes.</p>}
      {streams.filter((s) => !source || s.source === source).map((s) => <div className="probe-profile" key={s.id}>
        <strong>{sources[s.source]} · {s.name.split(":")[0] === "events" ? "Eventos" : s.name.split(":")[0] === "alerts" ? "Alertas actuales" : s.name.split(":")[0] === "logs" ? "Logs" : "Historial"}{!s.active && " · Configuración anterior"}</strong>
        <p>{coverage[s.coverage]}{s.reason ? ` · ${reasons[s.reason] || "Consulta con limitaciones"}` : ""}</p>
        <p>Ventana: {date(s.window_start)} — {date(s.window_end)}</p>
        <small>Último intento: {date(s.last_attempt_at)} · Última lectura válida: {date(s.last_success_at)} · Último evento: {date(s.last_event_at)}</small>
      </div>)}
    </section>
    {(!source || source === "graylog") && <LogSenders api={api} admin={admin} date={date} />}
  </>;
}

function LogSenders({ api, admin, date }: { api: Api; admin: boolean; date: (stamp: number | null) => string }) {
  const [rows, setRows] = useState<any[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const load = async () => {
    try { setRows(await api("/log-senders")); setError(""); } catch (e) { setError((e as Error).message); }
  };
  useEffect(() => { void load(); }, [api]);
  const assign = async (name: string, device_id: string | null) => {
    setBusy(true);
    try { await api("/log-senders/" + encodeURIComponent(name), "PUT", { device_id }); await load(); }
    catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  if (!rows.length && !error) return null;
  return <section className="panel probe-panel">
    <h2>Equipos que envían logs</h2>
    <p>Graylog identifica cada log por el nombre o la IP del equipo que lo envía. La asociación con el inventario se confirma aquí: las coincidencias por nombre o IP son solo sugerencias.</p>
    {error && <p role="alert">No se pudo actualizar: {error}</p>}
    <Paged items={rows}>{(page) => <div className="probe-table"><table>
      <thead><tr><th>Origen</th><th>IP de envío</th><th>Logs</th><th>Último log</th><th>Equipo del inventario</th></tr></thead>
      <tbody>{page.map((r) => <tr key={r.name}>
        <td>{r.name}</td><td className="mono">{r.address}</td><td>{r.messages}</td><td>{date(r.last_seen)}</td>
        <td><AssociationPicker api={api} admin={admin} row={r} busy={busy} onAssign={(id) => void assign(r.name, id)} /></td>
      </tr>)}</tbody>
    </table></div>}</Paged>
  </section>;
}
