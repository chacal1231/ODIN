import { useEffect, useState } from "react";
import { statusNames } from "./inventory";
type Api = (path: string, method?: string, body?: unknown) => Promise<any>;
type Props = { api: Api; onError: (s: string) => void; onNavigate: (page: string) => void };
const marks: Record<string, string> = { done: "✓", pending: "○", error: "!" };
const placeholders: Record<string, [string, string]> = {
  librenms: ["https://librenms.tuisp.net", "Token de API de LibreNMS (solo lectura)"],
  zabbix: ["https://zabbix.tuisp.net/api_jsonrpc.php", "Token de API de Zabbix (usuario de solo lectura)"],
  graylog: ["http://graylog.tuisp.net:9000", "Token de Graylog"],
  smartolt: ["https://tuisp.smartolt.com", "API key de SmartOLT (X-Token)"],
};
const runErrors: Record<string, string> = {
  access_denied: "El servidor rechazó el token", network_not_allowed: "La IP del servidor no está en las redes permitidas",
  dns_or_network_invalid: "No se pudo resolver el nombre", connection_failed: "No se pudo conectar", invalid_response: "Respuesta no válida",
  redirect_not_allowed: "El servidor redirige: usa la URL final", http_error: "El servidor devolvió un error", timeout: "Sin respuesta a tiempo",
};

/** Connect one source: URL, token and allowed networks, then test and (for inventory sources) synchronize. */
function SourceForm({ api, onError, kind, onDone }: { api: Api; onError: (s: string) => void; kind: string; onDone: () => void }) {
  const [row, setRow] = useState<any>(null), [runs, setRuns] = useState<any[]>([]);
  const [endpoint, setEndpoint] = useState(""), [secret, setSecret] = useState(""), [networks, setNetworks] = useState("");
  const [busy, setBusy] = useState(false), [message, setMessage] = useState("");
  const public_api = kind === "smartolt";
  const inventory = kind === "librenms" || kind === "zabbix";
  async function load() {
    const [rows, latest] = await Promise.all([api("/admin/integrations"), api("/inventory/runs")]);
    const found = rows.find((x: any) => x.kind === kind);
    setRow(found);
    setRuns(latest.filter((r: any) => r.source === kind));
    return found;
  }
  useEffect(() => {
    load().then((found) => {
      setEndpoint(found?.endpoint || "");
      setNetworks((found?.allowed_networks || []).join(", "));
    }).catch((e) => onError((e as Error).message));
    const timer = setInterval(() => void load().catch(() => {}), 4000);
    return () => clearInterval(timer);
  }, [kind]);
  async function act(fn: () => Promise<void>, done: string) {
    setBusy(true);
    onError("");
    try {
      await fn();
      setMessage(done);
      await load();
      onDone();
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const latest = runs[0];
  return (
    <>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void act(async () => {
            await api("/admin/integrations/" + kind, "PUT", {
              endpoint, secret: secret || undefined, version: row?.version,
              allowed_networks: public_api ? undefined : networks.split(",").map((x) => x.trim()).filter(Boolean),
              auto_sync: inventory ? true : undefined,
            });
            setSecret("");
          }, "Guardado. Ahora prueba la conexión.");
        }}
      >
        <div className="probe-form-grid">
          <label>URL<input type="url" required value={endpoint} placeholder={placeholders[kind][0]} onChange={(e) => setEndpoint(e.target.value)} /></label>
          <label>Token
            <input type="password" autoComplete="new-password" value={secret} onChange={(e) => setSecret(e.target.value)}
              placeholder={row?.has_secret ? "Guardado · vacío conserva el actual" : placeholders[kind][1]} />
          </label>
          {!public_api && (
            <label>Redes permitidas del servidor (CIDR)
              <input required value={networks} placeholder="10.0.0.15/32" onChange={(e) => setNetworks(e.target.value)} />
            </label>
          )}
        </div>
        {!public_api && <small>La IP (o red) donde vive {kind === "graylog" ? "Graylog" : kind === "zabbix" ? "Zabbix" : "LibreNMS"}. ODIN no conecta a ninguna otra.</small>}
        <div className="button-row spaced">
          <button disabled={busy}>Guardar</button>
          <button type="button" className="secondary" disabled={busy || !row?.has_secret}
            onClick={() => void act(() => api("/admin/integrations/" + kind + "/test", "POST"), "Prueba solicitada; el resultado aparece abajo en unos segundos.")}>
            Probar conexión
          </button>
          {inventory && (
            <button type="button" className="secondary" disabled={busy || !row?.checked_at}
              onClick={() => void act(() => api("/admin/integrations/" + kind + "/sync", "POST"), "Sincronización solicitada.")}>
              Sincronizar inventario
            </button>
          )}
        </div>
      </form>
      {message && <p role="status" className="muted">{message}</p>}
      <p>
        Estado: <span className="badge">{statusNames[row?.status || "not_configured"] || row?.status}</span>
        {latest && (
          <small>
            Última {latest.mode === "sync" ? "sincronización" : "prueba"}: {statusNames[latest.status] || latest.status}
            {latest.count != null && ` · ${latest.count} equipos`}
            {latest.error_code && ` · ${runErrors[latest.error_code] || latest.error_code}`}
          </small>
        )}
      </p>
    </>
  );
}

function Providers({ api, onError }: Props) {
  const [data, setData] = useState<any>(null);
  useEffect(() => { api("/setup/providers").then(setData).catch((e) => onError((e as Error).message)); }, []);
  if (!data) return <p className="muted">Leyendo los puertos…</p>;
  return (
    <>
      <p className="muted">
        {data.ports} puertos con IP · {data.groups.length} proveedores · {data.internal} enlaces internos · {data.customers} puertos de clientes
      </p>
      {data.groups.length > 0 && (
        <div className="probe-table">
          <table>
            <thead><tr><th>Tipo</th><th>Proveedor</th><th>Puertos</th></tr></thead>
            <tbody>
              {data.groups.map((g: any) => (
                <tr key={g.role + g.provider}>
                  <td><span className="badge">{g.label}</span></td>
                  <td>{g.provider}</td>
                  <td>{g.ports.map((p: any) => (
                    <small key={p.device_id + p.interface}>{p.device_name} <span className="mono">{p.interface}</span> · <span className="mono">{p.network}</span> · «{p.description}»</small>
                  ))}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {data.unclassified_count > 0 && (
        <>
          <h3>Puertos con IP sin rol reconocido ({data.unclassified_count})</h3>
          <p className="muted">Si alguno es un tránsito, transporte, IXP o peering, corrige su descripción en el router. LibreNMS la trae en la próxima sincronización.</p>
          <div className="probe-table">
            <table>
              <thead><tr><th>Equipo</th><th>Puerto</th><th>Red</th><th>Descripción actual</th></tr></thead>
              <tbody>
                {data.unclassified.map((p: any) => (
                  <tr key={p.device_id + p.interface}>
                    <td>{p.device_name}</td><td className="mono">{p.interface}</td><td className="mono">{p.network}</td><td>{p.description || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      <details open={data.groups.length === 0}>
        <summary>Convención de descripciones</summary>
        <div className="probe-table">
          <table>
            <thead><tr><th>Rol</th><th>Descripción del puerto</th></tr></thead>
            <tbody>
              <tr><td>Tránsito</td><td className="mono">TRANSITO-CARRIER-100G</td></tr>
              <tr><td>Transporte</td><td className="mono">TRANS-TX-POP-NORTE-RUTA-01 · TRANSPORTE-CENTRO-NORTE</td></tr>
              <tr><td>IXP</td><td className="mono">PIT-IXPBOG · IXP-NAP</td></tr>
              <tr><td>Peering</td><td className="mono">PNI-CDN-01-100G · PTP-PROVEEDOR · PEERING-X</td></tr>
              <tr><td>Cliente</td><td className="mono">CUST: Nombre {"{DIA:DIA-0001 | ASN:NA | CT:ISP}"} · DIA-0009-nombre</td></tr>
              <tr><td>CDN interno</td><td className="mono">OCA01-… · CDN-…</td></tr>
            </tbody>
          </table>
        </div>
        <p className="muted">La primera palabra es el rol y la segunda el proveedor; un transporte se muestra con su descripción completa. Un puerto cuya subred contiene una IP de otro equipo propio es un enlace interno.</p>
      </details>
    </>
  );
}

function Checks({ api, onError, onDone }: Props & { onDone: () => void }) {
  const [items, setItems] = useState<any[] | null>(null), [chosen, setChosen] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false), [result, setResult] = useState<any>(null);
  const load = () => api("/setup/suggestions").then((x) => { setItems(x); setChosen(new Set(x.map((s: any) => s.address))); });
  useEffect(() => { load().catch((e) => onError((e as Error).message)); }, []);
  if (!items) return <p className="muted">Buscando sugerencias…</p>;
  return (
    <>
      {items.length === 0 ? <p className="muted">No hay sugerencias nuevas: cada equipo y cada enlace con proveedores ya tiene su comprobación, o falta el inventario.</p> : (
        <div className="probe-table">
          <table>
            <thead><tr><th></th><th>Comprobación</th><th>Dirección</th><th>Por qué</th></tr></thead>
            <tbody>
              {items.map((s) => (
                <tr key={s.address}>
                  <td><input type="checkbox" aria-label={"Crear " + s.name} checked={chosen.has(s.address)} onChange={(e) => {
                    const next = new Set(chosen);
                    if (e.target.checked) next.add(s.address); else next.delete(s.address);
                    setChosen(next);
                  }} /></td>
                  <td>{s.name}</td>
                  <td className="mono">{s.address}</td>
                  <td><small>{s.kind === "device" ? `Gestión del equipo (${s.source})` : `Otro extremo del enlace de ${s.role === "transport" ? "transporte" : s.role === "ixp" ? "IXP" : s.role === "peering" ? "peering" : "tránsito"}`}</small></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {items.length > 0 && (
        <button disabled={busy || chosen.size === 0} onClick={async () => {
          setBusy(true);
          onError("");
          try {
            setResult(await api("/setup/suggestions", "POST", { items: items.filter((s) => chosen.has(s.address)).map((s) => ({ address: s.address, name: s.name })) }));
            await load();
            onDone();
          } catch (e) {
            onError((e as Error).message);
          } finally {
            setBusy(false);
          }
        }}>
          Crear {chosen.size} comprobaciones
        </button>
      )}
      {result && (
        <p role="status">
          {result.created.length} creadas y activas.
          {result.failed.map((f: any) => <small key={f.address}><span className="mono">{f.address}</span>: {f.error}</small>)}
        </p>
      )}
      <p className="muted">Sondean desde el servidor ODIN con ICMP: 10 paquetes cada 30 segundos, con la plantilla "PE / infraestructura propia" o "Enlaces y tránsito". Se ajustan después en Monitoreo → Red propia.</p>
    </>
  );
}

function StepBody({ step, props, reload }: { step: any; props: Props; reload: () => void }) {
  const { api, onError, onNavigate } = props;
  if (["librenms", "zabbix", "graylog", "smartolt"].includes(step.id))
    return (
      <>
        <SourceForm api={api} onError={onError} kind={step.id} onDone={reload} />
        {step.id === "librenms" && step.devices > 0 && <p>{step.devices} equipos y {step.ports} puertos importados.</p>}
        {step.id === "zabbix" && step.devices > 0 && <p>{step.devices} hosts importados. Activa sus alertas en <button className="link" onClick={() => onNavigate("Eventos")}>Eventos</button>.</p>}
        {step.id === "graylog" && step.status !== "not_configured" && !step.reading && (
          <p className="notice">Conexión lista. Activa la lectura de logs en <button className="link" onClick={() => onNavigate("Eventos")}>Eventos</button>.</p>
        )}
        {step.id === "smartolt" && step.status !== "not_configured" && !step.reading && (
          <p className="notice">Conexión lista. Indica la zona horaria y activa la lectura en <button className="link" onClick={() => onNavigate("OLTs")}>Monitoreo → OLTs</button>.</p>
        )}
        {step.id === "smartolt" && step.olts > 0 && <p>{step.olts} OLTs leídas.</p>}
      </>
    );
  if (step.id === "providers") return <Providers {...props} />;
  if (step.id === "checks") return <><p>{step.targets} comprobaciones activas.</p><Checks {...props} onDone={reload} /></>;
  if (step.id === "telegram")
    return (
      <ol className="setup-howto">
        <li className={step.configured ? "done" : ""}>Crea un bot con @BotFather y guarda su token en <button className="link" onClick={() => onNavigate("Administración")}>Administración → Fuentes → Telegram</button> (URL <span className="mono">https://api.telegram.org</span>).</li>
        <li>Cada persona vincula su cuenta desde su nombre → Mi acceso → Telegram, enviando <span className="mono">/vincular código</span> al bot.</li>
        <li className={step.group ? "done" : ""}>Agrega el bot al grupo del NOC y escribe <span className="mono">/grupo</span>.</li>
        <li className={step.providers_group ? "done" : ""}>En el grupo interno de proveedores, escribe <span className="mono">/proveedores</span>.</li>
      </ol>
    );
  return (
    <ol className="setup-howto">
      <li>Configura el modelo en <button className="link" onClick={() => onNavigate("Administración")}>Administración → Fuentes</button>: <b>Modelo local (llama.cpp)</b> o <b>DeepSeek (API)</b>. Con DeepSeek, lo analizado sale de tu red.</li>
      <li className={step.state === "done" ? "done" : ""}>Actívalo en <button className="link" onClick={() => onNavigate("Casos")}>Casos</button>.{step.provider && ` Activo: ${step.provider === "deepseek" ? "DeepSeek" : "modelo local"}.`}</li>
    </ol>
  );
}

export function Setup(props: Props) {
  const { api, onError } = props;
  const [data, setData] = useState<any>(null), [active, setActive] = useState<string | null>(null);
  const load = () => api("/setup").then((x) => {
    setData(x);
    setActive((current) => current || x.steps.find((s: any) => s.state !== "done")?.id || x.steps[0].id);
  }).catch((e) => onError((e as Error).message));
  useEffect(() => { void load(); }, []);
  if (!data) return <p className="muted">Consultando la puesta en marcha…</p>;
  const index = data.steps.findIndex((s: any) => s.id === active);
  const step = data.steps[index];
  return (
    <>
      <div className="title-row">
        <h1>Puesta en marcha</h1>
        <small>{data.done} de {data.total} pasos</small>
      </div>
      <p className="muted">Conecta las fuentes en este orden: cada una da sentido a la siguiente. Los pasos opcionales se pueden dejar para después.</p>
      <div className="setup">
        <ol className="setup-steps">
          {data.steps.map((s: any, n: number) => (
            <li key={s.id}>
              <button className={(s.id === active ? "selected " : "") + s.state} aria-current={s.id === active ? "step" : undefined} onClick={() => setActive(s.id)}>
                <span className="setup-mark" aria-label={s.state === "done" ? "Listo" : s.state === "error" ? "Con error" : "Pendiente"}>{marks[s.state]}</span>
                <span>{n + 1}. {s.title}{s.optional && <small> · opcional</small>}</span>
              </button>
            </li>
          ))}
        </ol>
        <section className="panel">
          <h2>{index + 1}. {step.title}</h2>
          <p>{step.purpose}</p>
          {step.last_error && <p className="error">Último error: {runErrors[step.last_error] || step.last_error}</p>}
          <StepBody key={step.id} step={step} props={props} reload={() => void load()} />
          <div className="button-row spaced">
            {index > 0 && <button className="secondary" onClick={() => setActive(data.steps[index - 1].id)}>Anterior</button>}
            {index < data.steps.length - 1 && <button className="secondary" onClick={() => { void load(); setActive(data.steps[index + 1].id); }}>Siguiente</button>}
          </div>
        </section>
      </div>
    </>
  );
}
