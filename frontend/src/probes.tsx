import { useEffect, useState, useRef, FormEvent } from "react";
import { Paged } from "./pager";
import { Traces } from "./traces";
import "./probes.css";
import { MonitoringNavigator, DestinationOrganization } from "./monitoring";
type Api = (path: string, method?: string, body?: unknown) => Promise<any>;
type Props = {
  api: Api;
  admin: boolean;
  onError: (s: string) => void;
  zone: string;
};
export const labels: Record<string, string> = {
  healthy: "Normal",
  partial: "Afectación parcial",
  unmonitored: "Sin comprobaciones esenciales",
  warning: "Advertencia",
  critical: "Crítico",
  warming: "Reuniendo muestras",
  no_data: "Sin datos",
  maintenance: "Mantenimiento",
  paused: "Pausado",
  pending: "Pendiente",
  running: "En curso",
  done: "Completado",
  failed: "Fallido",
  cancelled: "Cancelado",
  opened: "Apertura",
  recovered: "Recuperación",
  conditions_changed: "Cambio de condición",
  acknowledged: "Reconocido",
  observation: "Observación",
  configuration_changed: "Cambio de configuración",
  target_archived: "Destino archivado",
  maintenance_scheduled: "Mantenimiento programado",
  maintenance_cancelled: "Mantenimiento cancelado",
  loss: "Pérdida",
  availability: "Intentos fallidos",
  endpoint_unavailable: "Endpoint sin respuesta",
  pending_validation: "Pendiente de validación",
  latency: "Latencia",
  unreachable: "Sin respuesta",
  recovery_pending: "Comprobando recuperación",
};
const errors: Record<string, string> = {
  endpoint_check_failed: "El endpoint no respondió a todos los intentos",
  validation_failed: "No se pudo validar el destino",
  dns_non_public_address: "El DNS devolvió una dirección no pública",
  dns_address_limit: "Demasiadas direcciones DNS",
  probe_round_budget: "El sondeo excede el intervalo disponible",
  probe_local_network_error:
    "El servidor no tiene conectividad para este sondeo",
  probe_capacity: "Capacidad de sondeo excedida",
  probe_not_started: "Sondeador sin iniciar",
  probe_stale: "Sondeador sin señal",
  dns_failed: "No se pudo resolver el DNS",
  dns_timeout: "Tiempo de resolución agotado",
  dns_approval_required: "El DNS cambió. Revisa y autoriza sus IPs.",
  address_not_allowed: "Dirección no permitida",
  probe_timeout: "Sondeo interrumpido por tiempo",
  probe_execution_failed: "Error local al ejecutar el sondeo",
  probe_interrupted: "Ejecución interrumpida",
  probe_parse_failed: "Respuesta de sondeo no válida",
  probe_incomplete: "Medición incompleta",
  fping_unavailable: "Sondeador no disponible",
  probe_cycle_failed: "Error del sondeador",
};
const fmt = (n: number | null | undefined, d = 1) =>
  n == null ? "—" : n.toLocaleString("es", { maximumFractionDigits: d });
const when = (s: number | null, zone: string) =>
  s ? new Date(s * 1000).toLocaleString("es", { timeZone: zone }) : "—";
export const ProbeBadge = ({ state }: { state: string }) => (
  <span className={"badge probe-status " + state}>
    {labels[state] || state}
  </span>
);
const message = (e: unknown) =>
  errors[(e as Error).message] || (e as Error).message;
function usePoll(fn: () => Promise<void>, deps: unknown[]) {
  useEffect(() => {
    let active = true;
    const run = () => {
      if (active) void fn();
    };
    run();
    const timer = setInterval(run, 15000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, deps);
}
export function ProbeSummary({
  api,
  onError,
}: {
  api: Api;
  onError: (s: string) => void;
}) {
  const [data, setData] = useState<any>(null);
  usePoll(async () => {
    try {
      setData(await api("/probe-overview"));
    } catch (e) {
      setData(null);
      onError(message(e));
    }
  }, []);
  return (
    <>
      <div className="stats">
        {[
          ["Incidentes de sondeo activos", data?.incidents],
          ["Destinos vigilados", data?.targets],
          [
            "Destinos evaluados",
            data ? `${data.with_data} / ${data.targets}` : null,
          ],
        ].map(([name, value]) => (
          <section key={name}>
            <span>{name}</span>
            <strong>{value ?? "—"}</strong>
          </section>
        ))}
      </div>
      <p className="probe-origin">
        Sondeador ODIN <ProbeBadge state={data?.origin.state || "no_data"} />
        <small>Mediciones desde este servidor</small>
      </p>
    </>
  );
}
const configFields: [string, string, number, number, number][] = [
  ["interval_seconds", "Intervalo (s)", 30, 300, 30],
  ["packets", "Paquetes / intentos por ronda", 1, 20, 1],
  ["window_seconds", "Ventana (s)", 300, 1800, 30],
  ["loss_warning", "Pérdida / fallos · advertencia (%)", 0.1, 99, 0.1],
  ["loss_critical", "Pérdida / fallos · crítico (%)", 0.1, 100, 0.1],
  [
    "loss_recovery",
    "Pérdida / fallos · recuperación por debajo de (%)",
    0.1,
    99,
    0.1,
  ],
  ["rtt_warning", "Latencia p95 · advertencia (ms)", 0.1, 10000, 0.1],
  ["rtt_critical", "Latencia p95 · crítico (ms)", 0.1, 20000, 0.1],
  ["period_ms", "Separación de intentos (ms)", 100, 2000, 10],
  ["timeout_ms", "Tiempo de espera (ms)", 50, 2000, 10],
  ["minimum_coverage", "Cobertura mínima (0–1)", 0.5, 1, 0.05],
  ["persistence", "Evaluaciones para abrir o escalar", 1, 10, 1],
  ["recovery_rounds", "Evaluaciones para recuperar", 1, 10, 1],
  ["unreachable_rounds", "Rondas sin respuesta", 2, 10, 1],
  ["rtt_recovery_ratio", "Factor de recuperación de latencia", 0.5, 0.99, 0.01],
];
function ConfigFields({
  config,
  onChange,
}: {
  config: any;
  onChange: (x: any) => void;
}) {
  return (
    <div className="probe-form-grid">
      {configFields.map(([key, label, min, max, step]) => (
        <label key={key}>
          {label}
          <input
            type="number"
            min={min}
            max={max}
            step={step}
            required={!key.startsWith("rtt_") || key === "rtt_recovery_ratio"}
            value={config[key] ?? ""}
            placeholder={key.startsWith("rtt_") ? "Desactivado" : undefined}
            onChange={(e) =>
              onChange({
                ...config,
                [key]: e.target.value === "" ? null : Number(e.target.value),
              })
            }
          />
        </label>
      ))}
    </div>
  );
}
export function Destinations({
  api,
  admin,
  onError,
  zone,
  scope,
  focus,
}: Props & {
  scope: "network" | "external";
  focus?: { popId?: string; linkId?: string; destinationId?: string };
}) {
  const [organization, setOrganization] = useState<any>(null),
    [destinationId, setDestinationId] = useState(focus?.destinationId || ""),
    [allChecks, setAllChecks] = useState(false),
    [organizer, setOrganizer] = useState<any>(null);
  const [targets, setTargets] = useState<any[]>([]),
    [catalog, setCatalog] = useState<any[]>([]),
    [category, setCategory] = useState(""),
    [catalogSearch, setCatalogSearch] = useState(""),
    [profiles, setProfiles] = useState<any[]>([]),
    [pops, setPops] = useState<any[]>([]),
    [origin, setOrigin] = useState<any>(null);
  const [loaded, setLoaded] = useState(false),
    [busy, setBusy] = useState(false),
    [filter, setFilter] = useState(""),
    [editor, setEditor] = useState<any>(null),
    [selected, setSelected] = useState<string | null>(null),
    [profileEditor, setProfileEditor] = useState<any>(null);
  async function load() {
    const [m, p, o, c] = await Promise.all([
      api("/monitoring"),
      api("/probe-profiles"),
      api("/pops"),
      api("/target-categories"),
    ]);
    setCatalog(c);
    setOrganization(m);
    setTargets(m.destinations.flatMap((d: any) => d.checks));
    setProfiles(p);
    setPops(o);
    setOrigin(m.origin);
    setLoaded(true);
  }
  usePoll(async () => {
    try {
      await load();
    } catch (e) {
      setOrigin(null);
      setOrganization(null);
      setTargets((old) =>
        old.map((t) => ({
          ...t,
          state: t.enabled ? "no_data" : t.state,
          measurements: t.measurements.map((m: any) => ({
            ...m,
            state: "no_data",
          })),
        })),
      );
      onError(message(e));
    }
  }, []);
  async function run(fn: () => Promise<void>) {
    setBusy(true);
    onError("");
    try {
      await fn();
      await load();
    } catch (e) {
      onError(message(e));
    } finally {
      setBusy(false);
    }
  }
  function editTarget(value: any) {
    setEditor(value);
    requestAnimationFrame(() =>
      document
        .querySelector(".probe-editor")
        ?.scrollIntoView({ behavior: "smooth", block: "start" }),
    );
  }
  const initial = () => ({
    name: "",
    address: "",
    family: "ipv4",
    protocol: "icmp",
    dns_policy: "approved_ips",
    check_config: {
      port: 443,
      path: "/",
      method: "HEAD",
      expected_status: [
        200, 204, 301, 302, 303, 307, 308, 400, 401, 403, 404, 405,
      ],
    },
    pop_id: null,
    description: "",
    categories: scope === "network" ? ["red_propia"] : [],
    essential: true,
    destination_id: null,
    service: "",
    profile_version_id: profiles.find(
      (p) => p.id === "00000000-0000-4000-8000-000000000001",
    )?.versions[0]?.id,
    overrides: {},
    approved_ips: [],
    enabled: true,
  });
  const profile = profiles
    .flatMap((p) => p.versions)
    .find((v) => v.id === editor?.profile_version_id);
  const categoryName = (id: string) =>
    catalog.find((c) => c.id === id)?.name || id;
  const visibleTargets = targets.filter(
    (t) =>
      organization?.destinations.some(
        (d: any) =>
          d.scope === scope && d.checks.some((c: any) => c.id === t.id),
      ) &&
      (!destinationId || t.destination_id === destinationId) &&
      (!category ||
        (category === "unclassified"
          ? !t.categories?.length
          : t.categories?.includes(category))) &&
      [
        t.name,
        t.address,
        t.service,
        t.protocol,
        t.catalog_metadata?.region,
        t.catalog_metadata?.city,
        ...(t.categories || []).map(categoryName),
      ]
        .join(" ")
        .toLowerCase()
        .includes(filter.toLowerCase()),
  );
  return (
    <>
      <div className="title-row">
        <h1>{scope === "network" ? "Red propia" : "Servicios externos"}</h1>
        <button
          disabled={busy || !profiles.length || !admin}
          onClick={() => editTarget(initial())}
        >
          Nuevo destino
        </button>
      </div>
      <p className="probe-origin">
        Origen: ODIN <ProbeBadge state={origin?.state || "no_data"} />
        {origin?.error_code && (
          <small>{errors[origin.error_code] || origin.error_code}</small>
        )}
      </p>
      <MonitoringNavigator
        focus={focus}
        data={organization}
        scope={scope}
        selected={destinationId}
        admin={admin}
        onSelect={(id) => {
          setDestinationId(id);
          setSelected(null);
          setFilter("");
          setCategory("");
          if (id)
            requestAnimationFrame(() =>
              document
                .querySelector(".monitor-selected")
                ?.scrollIntoView({ behavior: "smooth", block: "start" }),
            );
        }}
        onConfigure={(d) => {
          setOrganizer(d);
          setEditor(null);
        }}
        onAdd={(d) => {
          const base = d.checks[0];
          setOrganizer(null);
          editTarget({
            ...initial(),
            destination_id: d.id,
            name: d.name.slice(0, 100) + " · nueva comprobación",
            address: base.address,
            family: base.family,
            dns_policy: base.dns_policy,
            approved_ips: base.approved_ips,
            pop_id: base.pop_id,
            categories: base.categories,
            service: base.service,
            enabled: false,
          });
        }}
      />
      {organizer && (
        <DestinationOrganization
          key={organizer.id + ":" + organizer.version}
          destination={organizer}
          data={organization}
          api={api}
          onSaved={load}
          onClose={() => setOrganizer(null)}
          onError={onError}
        />
      )}
      <div className="probe-actions">
        <label className="probe-check">
          <input
            type="checkbox"
            checked={allChecks}
            onChange={(e) => {
              setAllChecks(e.target.checked);
              setDestinationId("");
            }}
          />
          Mostrar todas las comprobaciones de esta vista
        </label>
      </div>
      <section className="panel probe-panel">
        <details className="destination-catalog">
          <summary>Catálogo de categorías y servicios</summary>
          <label>
            Buscar en el catálogo
            <input
              type="search"
              value={catalogSearch}
              onChange={(e) => setCatalogSearch(e.target.value)}
              placeholder="Categoría o servicio"
            />
          </label>
          <div className="category-catalog-grid">
            {catalog
              .filter((c) =>
                [c.name, ...c.services]
                  .join(" ")
                  .toLowerCase()
                  .includes(catalogSearch.toLowerCase()),
              )
              .map((c) => (
                <section className="category-card" key={c.id}>
                  <button
                    className="quiet"
                    onClick={() => {
                      setCategory(c.id);
                      setFilter("");
                    }}
                  >
                    {c.name}
                  </button>
                  <small>
                    {targets.filter((t) => t.categories?.includes(c.id)).length}{" "}
                    destinos registrados
                  </small>
                  <div className="category-services">
                    {c.services.map((service: string) => (
                      <button
                        key={service}
                        className="secondary"
                        disabled={!admin || !profiles.length || busy}
                        onClick={() =>
                          editTarget({
                            ...initial(),
                            categories: [c.id],
                            service,
                          })
                        }
                        title={"Preparar destino · " + service}
                      >
                        {service}
                      </button>
                    ))}
                  </div>
                  {c.hint && <small>{c.hint}</small>}
                </section>
              ))}
          </div>
        </details>
      </section>
      {(destinationId || allChecks) && (
        <section className="panel probe-panel">
          <h2>Comprobaciones</h2>
          <div className="probe-form-grid">
            <label>
              Categoría
              <select
                aria-label="Categoría"
                value={category}
                onChange={(e) => setCategory(e.target.value)}
              >
                <option value="">Todas las categorías</option>
                {catalog.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name} (
                    {targets.filter((t) => t.categories?.includes(c.id)).length}
                    )
                  </option>
                ))}
                <option value="unclassified">
                  Sin clasificar (
                  {targets.filter((t) => !t.categories?.length).length})
                </option>
              </select>
            </label>
            <label>
              Buscar destino
              <input
                type="search"
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                placeholder="Nombre, dirección o servicio"
              />
            </label>
          </div>
          <p className="muted">
            {visibleTargets.length} comprobaciones encontradas
          </p>
          <Paged items={visibleTargets}>{(page) => <div className="probe-table">
            <table>
              <thead>
                <tr>
                  <th>Comprobación</th>
                  <th>Clasificación</th>
                  <th>Estado</th>
                  <th>Latencia p95</th>
                  <th>Pérdida / fallos</th>
                  <th>Cobertura</th>
                  <th>Incidentes</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {page.map((t) => (
                  <tr key={t.id}>
                    <td>
                      <button
                        className="quiet"
                        onClick={() => setSelected(t.id)}
                      >
                        {t.name}
                      </button>
                      <small>
                        {(t.protocol || "icmp").toUpperCase()} · {t.address} ·{" "}
                        {pops.find((p) => p.id === t.pop_id)?.name || "Sin POP"}
                      </small>
                    </td>
                    <td>
                      <span>{t.service || "—"}</span>
                      <small>
                        {[t.catalog_metadata?.region, t.catalog_metadata?.city]
                          .filter(Boolean)
                          .join(" · ")}
                      </small>
                      <div className="category-tags">
                        {t.categories?.length ? (
                          t.categories.map((c: string) => (
                            <span key={c} className="badge">
                              {categoryName(c)}
                            </span>
                          ))
                        ) : (
                          <small>Sin clasificar</small>
                        )}
                      </div>
                    </td>
                    <td>
                      <ProbeBadge state={t.state} />
                      {(t.last_error || t.validation?.error) && (
                        <small>
                          {errors[t.last_error || t.validation?.error] ||
                            t.last_error ||
                            t.validation?.error}
                        </small>
                      )}
                    </td>
                    <td>
                      {t.measurements.map((m: any) => (
                        <small key={m.address}>
                          {t.measurements.length > 1 && m.address + ": "}
                          {m.state === "no_data" ? "—" : fmt(m.p95_ms) + " ms"}
                        </small>
                      ))}
                    </td>
                    <td>
                      {t.measurements.map((m: any) => (
                        <small key={m.address}>
                          {m.state === "no_data"
                            ? "—"
                            : fmt(
                                t.protocol === "icmp"
                                  ? m.loss_percent
                                  : m.failure_percent,
                              ) +
                              (t.protocol === "icmp"
                                ? " % pérdida"
                                : " % fallos")}
                        </small>
                      ))}
                    </td>
                    <td>
                      {t.measurements.map((m: any) => (
                        <small key={m.address}>
                          {m.state === "no_data"
                            ? "—"
                            : fmt(m.coverage * 100, 0) + " %"}
                        </small>
                      ))}
                    </td>
                    <td>{t.active_incidents}</td>
                    <td>
                      {admin && (
                        <button
                          className="secondary"
                          disabled={busy}
                          onClick={() =>
                            editTarget({ ...t, overrides: { ...t.overrides } })
                          }
                        >
                          Editar
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>}</Paged>
          {!targets.length && (
            <p className="muted">
              {loaded ? "No hay destinos registrados." : "Cargando destinos…"}
            </p>
          )}
        </section>
      )}
      {editor && (
        <section className="panel probe-panel probe-editor">
          <div className="title-row">
            <h2>
              {editor.id
                ? "Editar comprobación"
                : editor.destination_id
                  ? "Añadir comprobación"
                  : "Nuevo destino"}
            </h2>
            <button
              className="quiet"
              disabled={busy}
              onClick={() => setEditor(null)}
            >
              Cerrar
            </button>
          </div>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void run(async () => {
                const saved = await api(
                  editor.id ? "/targets/" + editor.id : "/targets",
                  editor.id ? "PUT" : "POST",
                  Object.fromEntries(
                    Object.entries(editor).filter(([k]) =>
                      [
                        "destination_id",
                        "essential",
                        "name",
                        "address",
                        "family",
                        "protocol",
                        "check_config",
                        "dns_policy",
                        "pop_id",
                        "description",
                        "categories",
                        "service",
                        "profile_version_id",
                        "overrides",
                        "approved_ips",
                        "enabled",
                        "version",
                      ].includes(k),
                    ),
                  ),
                );
                setDestinationId(saved.destination_id);
                setAllChecks(false);
                setCategory("");
                setFilter("");
                setEditor(null);
              });
            }}
          >
            <div className="probe-form-grid">
              <label>
                {editor.destination_id || editor.id
                  ? "Nombre de comprobación"
                  : "Nombre del destino"}
                <input
                  required
                  maxLength={120}
                  value={editor.name}
                  onChange={(e) =>
                    setEditor({ ...editor, name: e.target.value })
                  }
                />
              </label>
              <label>
                IP o nombre DNS
                <input
                  required
                  value={editor.address}
                  onChange={(e) =>
                    setEditor({
                      ...editor,
                      address: e.target.value,
                      approved_ips: [],
                    })
                  }
                />
              </label>
              <label>
                Protocolo
                <select
                  value={editor.protocol || "icmp"}
                  onChange={(e) => {
                    const protocol = e.target.value;
                    const p = profiles.find(
                      (p) =>
                        p.id ===
                        (protocol === "icmp"
                          ? "00000000-0000-4000-8000-000000000001"
                          : "00000000-0000-4000-8000-000000000003"),
                    );
                    setEditor({
                      ...editor,
                      protocol,
                      profile_version_id:
                        p?.versions[0]?.id || editor.profile_version_id,
                      overrides: {},
                      check_config: {
                        port: 443,
                        path: "/",
                        method: "HEAD",
                        expected_status: [
                          200, 204, 301, 302, 303, 307, 308, 400, 401, 403, 404,
                          405,
                        ],
                        ...editor.check_config,
                      },
                    });
                  }}
                >
                  <option value="icmp">ICMP · paquetes</option>
                  <option value="tcp">TCP · conexión</option>
                  <option value="https">HTTPS · respuesta</option>
                </select>
              </label>
              {editor.protocol !== "icmp" && (
                <label>
                  Puerto
                  <input
                    type="number"
                    min="1"
                    max="65535"
                    required
                    value={editor.check_config?.port || 443}
                    onChange={(e) =>
                      setEditor({
                        ...editor,
                        check_config: {
                          ...editor.check_config,
                          port: Number(e.target.value),
                        },
                      })
                    }
                  />
                </label>
              )}
              {editor.protocol === "https" && (
                <>
                  <label>
                    Ruta HTTPS
                    <input
                      required
                      value={editor.check_config?.path || "/"}
                      onChange={(e) =>
                        setEditor({
                          ...editor,
                          check_config: {
                            ...editor.check_config,
                            path: e.target.value,
                          },
                        })
                      }
                    />
                  </label>
                  <label>
                    Método
                    <select
                      value={editor.check_config?.method || "HEAD"}
                      onChange={(e) =>
                        setEditor({
                          ...editor,
                          check_config: {
                            ...editor.check_config,
                            method: e.target.value,
                          },
                        })
                      }
                    >
                      <option>HEAD</option>
                      <option>GET</option>
                    </select>
                  </label>
                  <label>
                    Códigos HTTP aceptados
                    <input
                      required
                      value={(editor.check_config?.expected_status || []).join(
                        ",",
                      )}
                      onChange={(e) =>
                        setEditor({
                          ...editor,
                          check_config: {
                            ...editor.check_config,
                            expected_status: e.target.value
                              .split(",")
                              .map(Number),
                          },
                        })
                      }
                    />
                  </label>
                </>
              )}
              <label>
                Actualización DNS
                <select
                  value={editor.dns_policy || "approved_ips"}
                  onChange={(e) =>
                    setEditor({ ...editor, dns_policy: e.target.value })
                  }
                >
                  <option value="approved_ips">Solo IPs autorizadas</option>
                  <option value="public_dns">Seguir DNS público</option>
                </select>
              </label>
              <label>
                Familia
                <select
                  value={editor.family}
                  onChange={(e) =>
                    setEditor({
                      ...editor,
                      family: e.target.value,
                      approved_ips: [],
                    })
                  }
                >
                  <option value="ipv4">IPv4</option>
                  <option value="ipv6">IPv6</option>
                </select>
              </label>
              <label>
                POP
                <select
                  value={editor.pop_id || ""}
                  onChange={(e) =>
                    setEditor({ ...editor, pop_id: e.target.value || null })
                  }
                >
                  <option value="">Sin POP</option>
                  {pops.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Perfil
                <select
                  value={editor.profile_version_id}
                  onChange={(e) =>
                    setEditor({ ...editor, profile_version_id: e.target.value })
                  }
                >
                  {profiles.flatMap((p) =>
                    p.versions.map((v: any) => (
                      <option key={v.id} value={v.id}>
                        {p.name} · v{v.number}
                      </option>
                    )),
                  )}
                </select>
              </label>
              <label>
                Descripción
                <input
                  maxLength={500}
                  value={editor.description}
                  onChange={(e) =>
                    setEditor({ ...editor, description: e.target.value })
                  }
                />
              </label>
            </div>
            <fieldset className="destination-classification">
              <legend>Clasificación</legend>
              <label>
                Servicio
                <input
                  maxLength={120}
                  list="destination-services"
                  value={editor.service || ""}
                  onChange={(e) =>
                    setEditor({ ...editor, service: e.target.value })
                  }
                  placeholder="Selecciona o escribe un servicio"
                />
              </label>
              <datalist id="destination-services">
                {[...new Set<string>(catalog.flatMap((c) => c.services))]
                  .sort()
                  .map((service) => (
                    <option key={service} value={service} />
                  ))}
              </datalist>
              <div className="category-options">
                {catalog.map((c) => (
                  <label key={c.id} className="probe-check">
                    <input
                      type="checkbox"
                      checked={editor.categories?.includes(c.id) || false}
                      onChange={(e) =>
                        setEditor({
                          ...editor,
                          categories: e.target.checked
                            ? [...(editor.categories || []), c.id]
                            : (editor.categories || []).filter(
                                (id: string) => id !== c.id,
                              ),
                        })
                      }
                    />
                    {c.name}
                  </label>
                ))}
              </div>
            </fieldset>
            <div className="probe-actions">
              <button
                type="button"
                className="secondary"
                disabled={busy || !editor.address}
                onClick={() =>
                  void run(async () => {
                    const x = await api("/targets/resolve", "POST", {
                      address: editor.address,
                      family: editor.family,
                    });
                    setEditor({ ...editor, approved_ips: x.addresses });
                  })
                }
              >
                Resolver dirección
              </button>
              <span>
                {editor.approved_ips.length
                  ? "IPs autorizadas al guardar: " +
                    editor.approved_ips.join(", ")
                  : editor.dns_policy === "public_dns"
                    ? "Se comprobarán las IPs públicas resueltas en cada ronda."
                    : "Para un DNS, resuelve sus IPs antes de guardar."}
              </span>
            </div>
            <label className="probe-check">
              <input
                type="checkbox"
                checked={editor.essential ?? true}
                onChange={(e) =>
                  setEditor({ ...editor, essential: e.target.checked })
                }
              />
              Esencial para el estado del destino
            </label>
            <label className="probe-check">
              <input
                type="checkbox"
                checked={editor.enabled}
                onChange={(e) =>
                  setEditor({ ...editor, enabled: e.target.checked })
                }
              />
              Vigilar destino
            </label>
            <details>
              <summary>Umbrales y ajustes del destino</summary>
              <p className="muted">
                Ajustes individuales:{" "}
                {Object.keys(editor.overrides)
                  .map((k) => configFields.find((f) => f[0] === k)?.[1] || k)
                  .join(", ") || "ninguno"}
                . Los campos modificados prevalecen sobre el perfil. Deja ambos
                RTT vacíos para medir sin alertas de latencia.
              </p>
              <ConfigFields
                config={{ ...profile?.config, ...editor.overrides }}
                onChange={(config) =>
                  setEditor({
                    ...editor,
                    overrides: Object.fromEntries(
                      Object.entries(config).filter(
                        ([key, value]) => value !== profile?.config[key],
                      ),
                    ),
                  })
                }
              />
              <button
                type="button"
                className="quiet"
                onClick={() => setEditor({ ...editor, overrides: {} })}
              >
                Usar valores del perfil
              </button>
            </details>
            <div className="probe-actions">
              <button disabled={busy}>
                {editor.id || editor.destination_id
                  ? "Guardar comprobación"
                  : "Guardar destino"}
              </button>
              {editor.id && (
                <button
                  type="button"
                  className="quiet danger"
                  disabled={busy}
                  onClick={() => {
                    if (
                      confirm(
                        "¿Archivar este destino? Su historial se conserva.",
                      )
                    )
                      void run(async () => {
                        await api(
                          "/targets/" + editor.id + "/archive",
                          "POST",
                          { version: editor.version },
                        );
                        setEditor(null);
                      });
                  }}
                >
                  Archivar
                </button>
              )}
            </div>
          </form>
        </section>
      )}
      {selected && (
        <TargetDetail
          key={selected}
          id={selected}
          target={targets.find((t) => t.id === selected)}
          api={api}
          admin={admin}
          onError={onError}
          zone={zone}
          onClose={() => setSelected(null)}
        />
      )}
      <details className="panel probe-panel">
        <summary>Perfiles de sondeo</summary>
        <div className="title-row">
          <h2>Perfiles reutilizables</h2>
          {admin && profiles.length > 0 && (
            <button
              className="secondary"
              onClick={() =>
                setProfileEditor({
                  name: "",
                  config: { ...profiles[0].versions[0].config },
                  apply_to_targets: false,
                })
              }
            >
              Nuevo perfil
            </button>
          )}
        </div>
        {profiles.map((p) => (
          <div className="probe-profile" key={p.id}>
            <span>
              <strong>{p.name}</strong> · v{p.version}
              <small>
                {p.versions.reduce((n: number, v: any) => n + v.targets, 0)}{" "}
                comprobaciones · {p.versions[0].config.interval_seconds} s ·
                pérdida / fallos {p.versions[0].config.loss_warning}/
                {p.versions[0].config.loss_critical} % · latencia{" "}
                {p.versions[0].config.rtt_warning == null
                  ? "sin umbral"
                  : `${p.versions[0].config.rtt_warning}/${p.versions[0].config.rtt_critical} ms`}
              </small>
            </span>
            {admin && (
              <button
                className="quiet"
                onClick={() =>
                  setProfileEditor({
                    ...p,
                    config: { ...p.versions[0].config },
                    apply_to_targets: false,
                  })
                }
              >
                Editar perfil
              </button>
            )}
          </div>
        ))}
      </details>
      {profileEditor && (
        <section className="panel probe-panel">
          <h2>
            {profileEditor.id ? "Nueva versión del perfil" : "Nuevo perfil"}
          </h2>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void run(async () => {
                await api(
                  profileEditor.id
                    ? "/probe-profiles/" + profileEditor.id
                    : "/probe-profiles",
                  profileEditor.id ? "PUT" : "POST",
                  {
                    name: profileEditor.name,
                    config: profileEditor.config,
                    version: profileEditor.version,
                    apply_to_targets: profileEditor.apply_to_targets,
                  },
                );
                setProfileEditor(null);
              });
            }}
          >
            <label>
              Nombre
              <input
                required
                maxLength={120}
                value={profileEditor.name}
                onChange={(e) =>
                  setProfileEditor({ ...profileEditor, name: e.target.value })
                }
              />
            </label>
            <ConfigFields
              config={profileEditor.config}
              onChange={(config) =>
                setProfileEditor({ ...profileEditor, config })
              }
            />
            {profileEditor.id && (
              <label className="probe-check">
                <input
                  type="checkbox"
                  checked={profileEditor.apply_to_targets}
                  onChange={(e) =>
                    setProfileEditor({
                      ...profileEditor,
                      apply_to_targets: e.target.checked,
                    })
                  }
                />
                Aplicar a sus{" "}
                {profileEditor.versions?.reduce(
                  (n: number, v: any) => n + v.targets,
                  0,
                )}{" "}
                destinos actuales, conservando ajustes individuales
              </label>
            )}
            <div className="probe-actions">
              <button disabled={busy}>Guardar versión</button>
              <button
                type="button"
                className="quiet"
                onClick={() => setProfileEditor(null)}
              >
                Cancelar
              </button>
            </div>
          </form>
        </section>
      )}
    </>
  );
}
function Plot({
  points,
  field,
  label,
  unit,
  zone,
  thresholds = [],
  maintenance = [],
}: {
  points: any[];
  field: string;
  label: string;
  unit: string;
  zone: string;
  thresholds?: number[];
  maintenance?: any[];
}) {
  const valid = points.map((p) => p[field]).filter((v) => v != null),
    max = ["loss", "failure"].includes(field)
      ? 100
      : Math.max(1, ...valid, ...thresholds) * 1.1;
  const start = points[0]?.time || 0,
    end = points.at(-1)?.time || start + 1;
  let drawing = "",
    open = false;
  for (const p of points) {
    if (p[field] == null || p.status !== "valid") {
      open = false;
      continue;
    }
    const x = 45 + ((p.time - start) / Math.max(1, end - start)) * 710,
      y = 145 - (p[field] / max) * 120;
    drawing += `${open ? " L" : " M"}${x.toFixed(2)},${y.toFixed(2)}`;
    open = true;
  }
  return (
    <figure className="probe-chart">
      <figcaption>
        {label} · {unit}
      </figcaption>
      <svg
        viewBox="0 0 800 180"
        role="img"
        aria-label={`${label}. ${valid.length} puntos con datos. Último: ${fmt(points.at(-1)?.[field])} ${unit}`}
      >
        {maintenance
          .filter(
            (m) =>
              m.starts_at < end &&
              Math.min(m.ends_at, m.cancelled_at || m.ends_at) > start,
          )
          .map((m) => (
            <rect
              key={m.id}
              className="chart-maintenance"
              x={
                45 +
                ((Math.max(start, m.starts_at) - start) /
                  Math.max(1, end - start)) *
                  710
              }
              y="25"
              width={Math.max(
                0,
                ((Math.min(end, m.ends_at, m.cancelled_at || m.ends_at) -
                  Math.max(start, m.starts_at)) /
                  Math.max(1, end - start)) *
                  710,
              )}
              height="120"
            >
              <title>{m.reason}</title>
            </rect>
          ))}
        {thresholds.map((t, i) => (
          <g key={i}>
            <path
              className={i ? "chart-critical" : "chart-warning"}
              d={`M45 ${145 - (t / max) * 120}H755`}
            />
            <text x="758" y={145 - (t / max) * 120 + 4}>
              {fmt(t)}
            </text>
          </g>
        ))}
        <path className="chart-grid" d="M45 25H755 M45 85H755 M45 145H755" />
        <text x="4" y="30">
          {fmt(max, 0)}
        </text>
        <text x="4" y="150">
          0
        </text>
        <path className="chart-line" d={drawing} />
        <text x="45" y="174">
          {points.length ? when(start, zone) : ""}
        </text>
        <text x="755" y="174" textAnchor="end">
          {points.length ? when(end, zone) : ""}
        </text>
      </svg>
      {!valid.length && (
        <p className="muted">Sin mediciones en este intervalo.</p>
      )}
    </figure>
  );
}
function TargetDetail({
  id,
  target,
  api,
  admin,
  onError,
  zone,
  onClose,
}: Props & { id: string; target: any; onClose: () => void }) {
  const [hours, setHours] = useState(1),
    [data, setData] = useState<any>(null),
    [seriesId, setSeriesId] = useState(""),
    [runs, setRuns] = useState<any[]>([]),
    [maintenance, setMaintenance] = useState<any[]>([]),
    [busy, setBusy] = useState(false);
  const requested = useRef({ id, hours });
  requested.current = { id, hours };
  async function load() {
    const [d, r, m] = await Promise.all([
      api(`/targets/${id}/measurements?hours=${hours}`),
      api(`/targets/${id}/probe-runs`),
      api(`/targets/${id}/maintenance`),
    ]);
    if (requested.current.id !== id || requested.current.hours !== hours)
      return;
    setData(d);
    setRuns(r);
    setMaintenance(m);
  }
  usePoll(async () => {
    try {
      await load();
    } catch (e) {
      setData(null);
      onError(message(e));
    }
  }, [id, hours]);
  async function run(fn: () => Promise<void>) {
    setBusy(true);
    onError("");
    try {
      await fn();
      await load();
    } catch (e) {
      onError(message(e));
    } finally {
      setBusy(false);
    }
  }
  const series =
    data?.series.find((s: any) => s.id === seriesId) || data?.series[0];
  return (
    <section className="panel probe-panel">
      <div className="title-row">
        <div>
          <h2>{target?.name || "Historial del destino"}</h2>
          <small>Medición desde ODIN · {target?.address}</small>
        </div>
        <button className="quiet" onClick={onClose}>
          Cerrar
        </button>
      </div>
      {target?.catalog_metadata?.provider && (
        <p className="muted">
          {[
            target.catalog_metadata.provider,
            target.catalog_metadata.region,
            target.catalog_metadata.city,
            target.catalog_metadata.country,
          ]
            .filter(Boolean)
            .join(" · ")}{" "}
          · ASN de referencia: {target.catalog_metadata.asn_reference}. Mide
          conectividad al endpoint, no disponibilidad de toda la región.
        </p>
      )}
      {target?.validation?.error && (
        <p className="notice">
          Validación:{" "}
          {errors[target.validation.error] || target.validation.error} ·{" "}
          {when(target.validation.at, zone)}
        </p>
      )}
      <div className="probe-actions">
        <label>
          Periodo
          <select
            value={hours}
            onChange={(e) => {
              setData(null);
              setHours(Number(e.target.value));
            }}
          >
            {[
              [1, "Última hora"],
              [6, "6 horas"],
              [24, "24 horas"],
              [72, "3 días"],
              [168, "7 días"],
              [720, "30 días"],
              [8760, "Un año"],
            ].map(([v, l]) => (
              <option key={v} value={v}>
                {l}
              </option>
            ))}
          </select>
        </label>
        <label>
          Serie por IP
          <select
            value={series?.id || ""}
            onChange={(e) => setSeriesId(e.target.value)}
          >
            {data?.series.map((s: any) => (
              <option key={s.id} value={s.id}>
                {s.address} · {when(s.started_at, zone)}
                {s.ended_at ? " · cerrada" : ""}
              </option>
            ))}
          </select>
        </label>
        {admin && (
          <button
            disabled={busy}
            className="secondary"
            onClick={() =>
              void run(async () => {
                await api(`/targets/${id}/probe-runs`, "POST");
              })
            }
          >
            Probar ahora
          </button>
        )}
      </div>
      {data?.truncated && (
        <p className="notice">
          Se muestran las 20 series más recientes. Reduce el periodo para
          consultar otras.
        </p>
      )}
      <Plot
        points={series?.points || []}
        field="rtt"
        label={`${(series?.config.protocol || target?.protocol || "icmp").toUpperCase()} · latencia ${data?.rtt_metric === "mean" ? "media" : "p95 de la ventana"}`}
        unit="ms"
        zone={zone}
        thresholds={[
          series?.config.rtt_warning,
          series?.config.rtt_critical,
        ].filter((x) => x != null)}
        maintenance={maintenance}
      />
      <Plot
        points={series?.points || []}
        field={
          (series?.config.protocol || "icmp") === "icmp" ? "loss" : "failure"
        }
        label={
          (series?.config.protocol || "icmp") === "icmp"
            ? "Pérdida de paquetes"
            : "Intentos fallidos"
        }
        unit="%"
        zone={zone}
        thresholds={[
          series?.config.loss_warning,
          series?.config.loss_critical,
        ].filter((x) => x != null)}
        maintenance={maintenance}
      />
      <p className="muted">
        Los huecos indican falta de mediciones; el sombreado marca
        mantenimiento. Líneas ámbar/roja: umbrales de advertencia/crítico.{" "}
        {data?.rtt_metric === "mean"
          ? "El histórico agregado muestra la media ponderada de latencia."
          : `Ventana de ${series?.config.window_seconds || target?.effective.window_seconds || 300} segundos.`}
      </p>
      <Traces api={api} targetId={id} zone={zone} onError={onError} />
      <details>
        <summary>Últimas ejecuciones</summary>
        <p className="muted">Las pruebas manuales no generan incidentes.</p>
        <Paged items={runs}>{(page) => <div className="probe-table">
          <table>
            <thead>
              <tr>
                <th>Hora</th>
                <th>Tipo</th>
                <th>Resultado</th>
              </tr>
            </thead>
            <tbody>
              {page.map((r) => (
                <tr key={r.id}>
                  <td>{when(r.slot, zone)}</td>
                  <td>{r.mode === "manual" ? "Manual" : "Programada"}</td>
                  <td>
                    <ProbeBadge state={r.status} />
                    {r.error_code && (
                      <small>{errors[r.error_code] || r.error_code}</small>
                    )}
                    {r.result?.rounds?.map((x: any) => (
                      <small key={x.address}>
                        {x.address} · {x.received}/{x.sent} respuestas ·{" "}
                        {(r.result?.protocol || "icmp") === "icmp"
                          ? "pérdida"
                          : "fallos"}{" "}
                        {fmt((100 * (x.sent - x.received)) / x.sent)} %
                      </small>
                    ))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>}</Paged>
      </details>
      <details>
        <summary>Mantenimiento</summary>
        {maintenance
          .filter((m) => !m.cancelled && m.ends_at > Date.now() / 1000)
          .map((m) => (
            <div className="probe-profile" key={m.id}>
              <span>
                {m.reason}
                <small>
                  {when(m.starts_at, zone)} – {when(m.ends_at, zone)}
                </small>
              </span>
              {admin && (
                <button
                  disabled={busy}
                  className="quiet"
                  onClick={() =>
                    void run(async () => {
                      await api("/maintenance/" + m.id + "/cancel", "POST");
                    })
                  }
                >
                  Cancelar
                </button>
              )}
            </div>
          ))}
        {admin && (
          <form
            onSubmit={(e: FormEvent<HTMLFormElement>) => {
              e.preventDefault();
              const f = new FormData(e.currentTarget);
              void run(async () => {
                await api(`/targets/${id}/maintenance`, "POST", {
                  starts_at: Math.floor(Date.now() / 1000),
                  ends_at:
                    Math.floor(Date.now() / 1000) +
                    Number(f.get("minutes")) * 60,
                  reason: f.get("reason"),
                });
              });
            }}
          >
            <div className="probe-form-grid">
              <label>
                Duración desde ahora (minutos)
                <input
                  name="minutes"
                  type="number"
                  min="1"
                  max="10080"
                  defaultValue="60"
                  required
                />
              </label>
              <label>
                Motivo
                <input name="reason" required maxLength={300} />
              </label>
            </div>
            <button disabled={busy}>Iniciar mantenimiento</button>
          </form>
        )}
      </details>
    </section>
  );
}
export function Incidents({
  api,
  onError,
  zone,
  focusId,
  onOpenCase,
}: Props & { focusId?: string; onOpenCase?: (id: string) => void }) {
  const [groups, setGroups] = useState<any[]>([]);
  usePoll(async () => {
    try {
      setGroups((await api("/monitoring")).incident_groups);
    } catch (e) {
      setGroups([]);
      onError(message(e));
    }
  }, []);
  const [after, setAfter] = useState(""),
    [state, setState] = useState("active"),
    [items, setItems] = useState<any[]>([]),
    [cursor, setCursor] = useState<string | null>(null),
    [id, setId] = useState<string | null>(focusId || null),
    [detail, setDetail] = useState<any>(null),
    [busy, setBusy] = useState(false),
    [note, setNote] = useState("");
  async function load() {
    const x = await api(
      "/incidents?state=" +
        state +
        (after ? "&after=" + encodeURIComponent(after) : ""),
    );
    setItems(x.items);
    setCursor(x.next_cursor);
  }
  usePoll(async () => {
    try {
      await load();
    } catch (e) {
      onError(message(e));
    }
  }, [state, after]);
  const selectedIncident = useRef(id);
  selectedIncident.current = id;
  usePoll(async () => {
    try {
      if (id) {
        const result = await api("/incidents/" + id);
        if (selectedIncident.current === id) setDetail(result);
      }
    } catch (e) {
      if (selectedIncident.current === id) {
        setDetail(null);
        onError(message(e));
      }
    }
  }, [id]);
  async function act(fn: () => Promise<void>) {
    setBusy(true);
    onError("");
    try {
      await fn();
      await load();
      if (id) {
        const result = await api("/incidents/" + id);
        if (selectedIncident.current === id) setDetail(result);
      }
    } catch (e) {
      onError(message(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <div className="title-row">
        <h1>Incidentes</h1>
        <select
          aria-label="Estado del incidente"
          value={state}
          onChange={(e) => {
            setItems([]);
            setAfter("");
            setState(e.target.value);
          }}
        >
          <option value="active">Activos</option>
          <option value="recovered">Recuperados</option>
          <option value="all">Todos</option>
        </select>
      </div>
      {state === "active" && (
        <section className="panel probe-panel">
          <h2>Agrupados por POP o destino</h2>
          <p className="muted">
            Agrupación operativa; no confirma una causa común.
          </p>
          <div className="monitor-destination-grid">
            {groups.map((g) => (
              <article className="monitor-destination" key={g.id}>
                <div className="title-row">
                  <strong>{g.name}</strong>
                  <ProbeBadge state={g.severity} />
                </div>
                {g.items.map((i: any) => (
                  <button
                    className="monitor-signal quiet"
                    key={i.id}
                    onClick={() => {
                      setDetail(null);
                      setId(i.id);
                      setNote("");
                    }}
                  >
                    <span>
                      {i.target_name} · {i.address}
                    </span>
                    <small>
                      {Object.keys(i.conditions)
                        .map((k) => labels[k] || k)
                        .join(", ")}{" "}
                      · {when(i.opened_at, zone)}
                    </small>
                  </button>
                ))}
              </article>
            ))}
          </div>
          {!groups.length && <p className="muted">Sin grupos activos.</p>}
        </section>
      )}
      <section className="panel probe-panel">
        <div className="probe-table">
          <table>
            <thead>
              <tr>
                <th>Destino</th>
                <th>Condición</th>
                <th>Apertura</th>
                <th>Estado</th>
                <th>Seguimiento</th>
              </tr>
            </thead>
            <tbody>
              {items.map((i) => (
                <tr key={i.id}>
                  <td>
                    <button
                      className="quiet"
                      onClick={() => {
                        setDetail(null);
                        setId(i.id);
                        setNote("");
                      }}
                    >
                      {i.target_name}
                    </button>
                    <small>{i.address}</small>
                  </td>
                  <td>
                    {Object.keys(i.conditions)
                      .map((k) => labels[k] || k)
                      .join(", ") || "Recuperado"}
                  </td>
                  <td>{when(i.opened_at, zone)}</td>
                  <td>
                    <ProbeBadge
                      state={i.closed_at ? "recovered" : i.severity}
                    />
                  </td>
                  <td>{i.acknowledged_at ? "Reconocido" : "Pendiente"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!items.length && (
          <p className="muted">Sin incidentes en esta vista.</p>
        )}
        {cursor && (
          <button
            className="secondary"
            disabled={busy}
            onClick={() => {
              setAfter(cursor);
              setItems([]);
            }}
          >
            Más incidentes
          </button>
        )}
      </section>
      {detail && (
        <section className="panel probe-panel">
          <div className="title-row">
            <h2>
              {detail.target_name} · {detail.address}
            </h2>
            <button
              className="quiet"
              onClick={() => {
                setId(null);
                setDetail(null);
              }}
            >
              Cerrar
            </button>
          </div>
          <p>
            Estado actual del destino:{" "}
            <ProbeBadge state={detail.target.state} />
          </p>
          <p>
            Apertura: {when(detail.opened_at, zone)} · Recuperación:{" "}
            {when(detail.closed_at, zone)}
          </p>
          {detail.case_id && onOpenCase && (
            <p>
              <button className="secondary" onClick={() => onOpenCase(detail.case_id)}>
                Ver caso e investigar
              </button>
            </p>
          )}
          <p className="muted">
            Umbrales al abrir: pérdida {detail.profile.loss_warning}/
            {detail.profile.loss_critical} % · latencia{" "}
            {detail.profile.rtt_warning == null
              ? "sin umbral"
              : `${detail.profile.rtt_warning}/${detail.profile.rtt_critical} ms`}
          </p>
          <button
            className="secondary"
            disabled={busy || !!detail.acknowledged_at}
            onClick={() =>
              void act(async () => {
                await api("/incidents/" + id + "/acknowledgments", "POST");
              })
            }
          >
            {detail.acknowledged_at ? "Reconocido" : "Reconocer incidente"}
          </button>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void act(async () => {
                await api("/incidents/" + id + "/observations", "POST", {
                  text: note,
                });
                setNote("");
              });
            }}
          >
            <label>
              Observación
              <textarea
                required
                maxLength={2000}
                value={note}
                onChange={(e) => setNote(e.target.value)}
              />
            </label>
            <button disabled={busy || !note.trim()}>Añadir observación</button>
          </form>
          {detail.traces?.length > 0 && (
            <Traces api={api} items={detail.traces} zone={zone} onError={onError} />
          )}
          <ol className="probe-timeline">
            {detail.events.map((e: any) => (
              <li key={e.id}>
                <strong>{labels[e.kind] || e.kind}</strong>
                <small>{when(e.at, zone)}</small>
                {e.data.text && <p>{e.data.text}</p>}
                {e.data.metrics && (
                  <p>
                    Latencia p95 {fmt(e.data.metrics.p95_ms)} ms ·{" "}
                    {e.data.metrics.failure_percent != null
                      ? "fallos"
                      : "pérdida"}{" "}
                    {fmt(
                      e.data.metrics.failure_percent ??
                        e.data.metrics.loss_percent,
                    )}{" "}
                    % · cobertura {fmt(e.data.metrics.coverage * 100, 0)} %
                  </p>
                )}
              </li>
            ))}
          </ol>
          {detail.events_truncated && (
            <p className="muted">Se muestran los últimos 200 registros.</p>
          )}
        </section>
      )}
    </>
  );
}
