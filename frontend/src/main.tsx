import React, { useState, useEffect, FormEvent } from "react";
import { createRoot } from "react-dom/client";
import qrcode from "qrcode-generator";
import "./style.css";
import { Brand, ThemePicker, useTheme } from "./branding";
import { Destinations, Incidents } from "./probes";
import { SourceEvents } from "./events";
import { Cases } from "./cases";
import { Chat, ChatCase } from "./chat";
import { OperationsOverview, StatusBar, useMonitoring } from "./monitoring";
import { Inventory, IntegrationManager, statusNames } from "./inventory";

type User = {
  id: string;
  username: string;
  role: string;
  active: boolean;
  mfa_confirmed: boolean;
};
type Challenge = {
  csrf_token: string;
  enrollment_required: boolean;
  password_change_required: boolean;
  mfa_secret?: string;
  provisioning_uri?: string;
};
let csrf = "";
// Drawn in the browser: the TOTP secret never goes to a QR service.
function TotpQr({ uri }: { uri: string }) {
  const qr = qrcode(0, "M");
  qr.addData(uri);
  qr.make();
  const n = qr.getModuleCount();
  let d = "";
  for (let r = 0; r < n; r++)
    for (let c = 0; c < n; c++) if (qr.isDark(r, c)) d += `M${c} ${r}h1v1h-1z`;
  return (
    <svg
      className="totp-qr"
      viewBox={`-4 -4 ${n + 8} ${n + 8}`}
      role="img"
      aria-label="Código QR para el autenticador"
      shapeRendering="crispEdges"
    >
      <rect x={-4} y={-4} width={n + 8} height={n + 8} fill="#fff" />
      <path d={d} fill="#000" />
    </svg>
  );
}
async function api(path: string, method = "GET", body?: unknown) {
  const response = await fetch("/api/v1" + path, {
    method,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401 && !path.startsWith("/auth/") && path !== "/me")
      window.dispatchEvent(new Event("session-expired"));
    throw new Error(
      typeof result.detail === "string"
        ? result.detail
        : "Revisa los campos e intenta nuevamente.",
    );
  }
  if (result.csrf_token) csrf = result.csrf_token;
  return result;
}
// Grouped by how the NOC works: operate, watch, check the sources, configure.
const sections: [string, string[]][] = [
  ["", ["Inicio"]],
  ["Operación", ["Casos", "Incidentes", "Chat"]],
  ["Monitoreo", ["Red propia", "Servicios externos"]],
  ["Fuentes", ["Eventos"]],
  ["Configuración", ["Mapa e inventario", "Administración"]],
];
const sectionIcons: Record<string, string> = {
  Inicio: "◈", Casos: "⚑", Incidentes: "◷", Chat: "✉", "Red propia": "◎", "Servicios externos": "◌",
  Eventos: "◇", "Mapa e inventario": "▦", Administración: "⚙",
};
const names: Record<string, string> = {
  zabbix: "Zabbix",
  librenms: "LibreNMS",
  graylog: "Graylog",
  llama_server: "Qwen local",
};

function App() {
  const { theme, setTheme } = useTheme();
  const [user, setUser] = useState<User | null>(null),
    [loading, setLoading] = useState(true),
    [page, setPage] = useState("Inicio");
  const [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [challenge, setChallenge] = useState<Challenge | null>(null);
  const group = sections.find(([, items]) => items.includes(page))?.[0];
  const [monitorFocus, setMonitorFocus] = useState<{
    popId?: string;
    linkId?: string;
    destinationId?: string;
  }>({});
  const [incidentFocus, setIncidentFocus] = useState("");
  const [caseFocus, setCaseFocus] = useState("");
  const [chatCase, setChatCase] = useState<ChatCase | null>(null);
  const monitor = useMonitoring(api, !!user);
  function go(next: string) {
    setMonitorFocus({});
    setIncidentFocus("");
    setCaseFocus("");
    setChatCase(null);
    setPage(next);
    setError("");
  }
  const [codes, setCodes] = useState<string[]>([]),
    [sources, setSources] = useState<{ kind: string; status: string }[]>([]);
  const [zone, setZone] = useState(
      Intl.DateTimeFormat().resolvedOptions().timeZone,
    ),
    [updated, setUpdated] = useState<Date | null>(null);
  async function refresh() {
    try {
      setSources(await api("/integration-health"));
      setUpdated(new Date());
    } catch (e) {
      setError((e as Error).message);
    }
  }
  useEffect(() => {
    api("/me")
      .then((x) => setUser(x.user))
      .catch(() => {})
      .finally(() => setLoading(false));
    const expire = () => {
      setUser(null);
      setChallenge(null);
      csrf = "";
      setError("La sesión expiró. Vuelve a ingresar.");
    };
    window.addEventListener("session-expired", expire);
    return () => window.removeEventListener("session-expired", expire);
  }, []);
  // Links from Telegram open one case: /#caso=<id>.
  useEffect(() => {
    const found = /^#caso=([\w-]{36})$/.exec(window.location.hash);
    if (!user || !found) return;
    go("Casos"); setCaseFocus(found[1]);
    history.replaceState(null, "", window.location.pathname);
  }, [user]);
  useEffect(() => {
    if (!user) return;
    void refresh();
    const timer=setInterval(()=>void refresh(),15000);
    return ()=>clearInterval(timer);
  }, [user]);
  async function login(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setBusy(true);
    setError("");
    const f = new FormData(e.currentTarget);
    try {
      if (!challenge) {
        setChallenge(
          await api("/auth/login", "POST", {
            username: f.get("username"),
            password: f.get("password"),
          }),
        );
      } else {
        const x = await api("/auth/mfa/verify", "POST", {
          code: f.get("code"),
          new_password: challenge.password_change_required
            ? f.get("new_password")
            : undefined,
        });
        setUser(x.user);
        setCodes(x.recovery_codes);
        setChallenge(null);
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  if (loading) return <div className="loading">Conectando con ODIN…</div>;
  if (!user)
    return (
      <main className="login">
        <div className="login-story">
          <Brand full />
        </div>
        <section className="login-card">
          <div className="login-appearance">
            <ThemePicker theme={theme} onChange={setTheme} />
          </div>
          <Brand />
          <h2>{challenge ? "Verifica tu identidad" : "Iniciar sesión"}</h2>
          {challenge && (
            <p className="muted">
              Introduce el código de tu autenticador o un código de
              recuperación.
            </p>
          )}
          {error && (
            <p role="alert" className="error">
              {error}
            </p>
          )}
          <form onSubmit={login}>
            {!challenge ? (
              <>
                <label>
                  Usuario
                  <input
                    name="username"
                    autoComplete="username"
                    required
                    maxLength={80}
                  />
                </label>
                <label>
                  Contraseña
                  <input
                    type="password"
                    name="password"
                    autoComplete="current-password"
                    required
                    maxLength={256}
                  />
                </label>
              </>
            ) : (
              <>
                {challenge.enrollment_required && (
                  <div className="notice">
                    <strong>Configura tu autenticador</strong>
                    <p>
                      Escanea el código con tu autenticador. Si no puedes,
                      añade una cuenta TOTP con la clave. Solo se muestra
                      durante el registro.
                    </p>
                    {challenge.provisioning_uri && (
                      <TotpQr uri={challenge.provisioning_uri} />
                    )}
                    <code className="secret">{challenge.mfa_secret}</code>
                  </div>
                )}
                {challenge.password_change_required && (
                  <label>
                    Nueva contraseña · mínimo 14 caracteres
                    <input
                      name="new_password"
                      type="password"
                      autoComplete="new-password"
                      minLength={14}
                      maxLength={256}
                      required
                    />
                  </label>
                )}
                <label>
                  Código de verificación
                  <input
                    name="code"
                    autoComplete="one-time-code"
                    required
                    maxLength={64}
                  />
                </label>
              </>
            )}
            <button disabled={busy}>
              {busy
                ? "Verificando…"
                : challenge
                  ? "Verificar e ingresar"
                  : "Continuar"}
            </button>
          </form>
          {challenge && (
            <button
              className="quiet"
              onClick={() => {
                setChallenge(null);
                setError("");
              }}
            >
              Volver al inicio
            </button>
          )}
        </section>
      </main>
    );
  return (
    <div className="app">
      <aside>
        <Brand />
        <nav>
          {sections.map(([label, items]) => (
            <div className="nav-group" key={label || "inicio"} role="group" aria-label={label || undefined}>
              {label && <small aria-hidden="true">{label}</small>}
              {items
                .filter((x) => x !== "Administración" || user.role === "admin")
                .map((x) => (
                  <button
                    key={x}
                    className={page === x ? "selected" : ""}
                    aria-current={page === x ? "page" : undefined}
                    onClick={() => go(x)}
                  >
                    <span aria-hidden="true">{sectionIcons[x]}</span>
                    {x}
                  </button>
                ))}
            </div>
          ))}
        </nav>
      </aside>
      <div className="workspace">
        <header>
          <span>
            Infraestructura / {group && group + " / "}<strong>{page}</strong>
          </span>
          <div className="header-actions">
            <ThemePicker theme={theme} onChange={setTheme} />
            <select
              aria-label="Zona horaria"
              value={zone}
              onChange={(e) => setZone(e.target.value)}
            >
              {[...new Set([zone, "UTC", "America/Bogota"])].map((z) => (
                <option key={z}>{z}</option>
              ))}
            </select>
            <span>
              <button className="quiet" onClick={() => setPage("Mi acceso")}>
                {user.username}
              </button>{" "}
              <small>
                {user.role === "admin" ? "Administrador" : "Operador"}
              </small>
            </span>
            <button
              className="quiet"
              onClick={async () => {
                try {
                  await api("/auth/logout", "POST");
                  setUser(null);
                  setCodes([]);
                  csrf = "";
                } catch (e) {
                  setError((e as Error).message);
                }
              }}
            >
              Salir
            </button>
          </div>
        </header>
        <StatusBar monitor={monitor} onNavigate={go} />
        <main className="content">
          {error && (
            <p role="alert" className="error">
              {error}
            </p>
          )}
          {codes.length > 0 && (
            <section className="notice recovery">
              <h2>Guarda tus códigos de recuperación</h2>
              <p>
                Cada código funciona una sola vez. Guárdalos fuera de ODIN antes
                de cerrar este aviso.
              </p>
              <div className="codes">
                {codes.map((c) => (
                  <code key={c}>{c}</code>
                ))}
              </div>
              <button onClick={() => setCodes([])}>Ya los guardé</button>
            </section>
          )}
          {page === "Inicio" ? (
            <>
              <div className="title-row">
                <div>
                  <h1>Centro de operaciones</h1>
                </div>
                <button
                  className="secondary"
                  onClick={() => {
                    void refresh();
                    void monitor.reload();
                  }}
                >
                  Actualizar
                </button>
              </div>
              <OperationsOverview
                monitor={monitor}
                onNavigate={go}
                onOpenPop={(popId) => {
                  go("Red propia");
                  setMonitorFocus({ popId });
                }}
                onOpenIncident={(id) => {
                  go("Incidentes");
                  setIncidentFocus(id);
                }}
                onOpenDestination={(scope, destinationId) => {
                  go(scope === "network" ? "Red propia" : "Servicios externos");
                  setMonitorFocus({ destinationId });
                }}
                zone={zone}
              />
              <section className="panel">
                <div className="title-row">
                  <h2>Fuentes de información</h2>
                  <small>
                    {updated
                      ? "Consultado " +
                        new Intl.DateTimeFormat("es", {
                          timeStyle: "medium",
                          timeZone: zone,
                        }).format(updated)
                      : "Sin actualizar"}
                  </small>
                </div>
                <div className="sources">
                  {sources.map((s) => (
                    <div key={s.kind}>
                      <strong>{names[s.kind]}</strong>
                      <span className="badge">
                        {statusNames[s.status] || s.status}
                      </span>
                    </div>
                  ))}
                </div>
              </section>
            </>
          ) : page === "Mapa e inventario" ? (
            <Inventory
              onAsk={(c) => { go("Chat"); setChatCase(c); }}
              onMonitor={(focus) => {
                setMonitorFocus(focus);
                setPage("Red propia");
              }}
              api={api}
              admin={user.role === "admin"}
              onError={setError}
            />
          ) : page === "Red propia" || page === "Servicios externos" ? (
            <Destinations
              key={page}
              focus={monitorFocus}
              scope={page === "Red propia" ? "network" : "external"}
              api={api}
              admin={user.role === "admin"}
              onError={setError}
              zone={zone}
            />
          ) : page === "Incidentes" ? (
            <Incidents
              key={incidentFocus || "all"}
              focusId={incidentFocus || undefined}
              onOpenCase={(id) => { go("Casos"); setCaseFocus(id); }}
              api={api}
              admin={user.role === "admin"}
              onError={setError}
              zone={zone}
            />
          ) : page === "Eventos" ? (
            <SourceEvents api={api} zone={zone} admin={user.role === "admin"} />
          ) : page === "Casos" ? (
            <Cases key={caseFocus || "all"} api={api} zone={zone} admin={user.role === "admin"} focusId={caseFocus || undefined}
              onAsk={(c) => { go("Chat"); setChatCase(c); }} />
          ) : page === "Chat" ? (
            <Chat api={api} zone={zone} initialCase={chatCase} links={{
              onOpenCase: (id) => { go("Casos"); setCaseFocus(id); },
              onOpenIncident: (id) => { go("Incidentes"); setIncidentFocus(id); },
              onGo: go,
            }} />
          ) : page === "Mi acceso" ? (
            <Recovery onError={setError} onCodes={setCodes} />
          ) : page === "Administración" ? (
            <Admin onError={setError} onCodes={setCodes} />
          ) : (
            <>
              <span className="eyebrow">{page.toUpperCase()}</span>
              <h1>{page}</h1>
              <section className="empty">
                <div className="empty-icon">◇</div>
                <span className="badge">Próximamente</span>
              </section>
            </>
          )}
        </main>
      </div>
    </div>
  );
}
function Admin({
  onError,
}: {
  onError: (s: string) => void;
  onCodes: (c: string[]) => void;
}) {
  const [users, setUsers] = useState<User[]>([]),
    [audit, setAudit] = useState<
      { id: string; action: string; created_at: number }[]
    >([]),
    [temporary, setTemporary] = useState(""),
    [busy, setBusy] = useState(false);
  async function refresh() {
    setUsers(await api("/admin/users"));
    setAudit(await api("/admin/audit"));
  }
  useEffect(() => {
    refresh().catch((e) => onError(e.message));
  }, []);
  async function run(fn: () => Promise<void>) {
    setBusy(true);
    onError("");
    try {
      await fn();
      await refresh();
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <h1>Administración</h1>
      <div className="admin-grid">
        <section className="panel">
          <h2>Usuarios</h2>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              const f = new FormData(e.currentTarget);
              void run(async () => {
                const x = await api("/admin/users", "POST", {
                  username: f.get("username"),
                  role: f.get("role"),
                });
                setTemporary(x.temporary_password);
              });
            }}
          >
            <label>
              Nombre de usuario
              <input name="username" pattern="[a-zA-Z0-9._-]{3,80}" required />
            </label>
            <label>
              Rol
              <select name="role">
                <option value="operator">Operador</option>
                <option value="admin">Administrador</option>
              </select>
            </label>
            <button disabled={busy}>Crear usuario</button>
          </form>
          {temporary && (
            <div className="notice">
              <p>
                Entrega esta contraseña temporal por un canal seguro. Se exigirá
                cambiarla y registrar MFA.
              </p>
              <code className="secret">{temporary}</code>
              <button className="quiet" onClick={() => setTemporary("")}>
                Ocultar
              </button>
            </div>
          )}
          <div className="user-list">
            {users.map((u) => (
              <div key={u.id}>
                <strong>{u.username}</strong>
                <small>
                  {u.role} · {u.active ? "Activo" : "Desactivado"}
                </small>
                <button
                  disabled={busy}
                  className="quiet"
                  onClick={() =>
                    void run(async () => {
                      await api(
                        "/admin/users/" + u.id + "/revoke-sessions",
                        "POST",
                      );
                    })
                  }
                >
                  Revocar sesiones
                </button>
                {u.active && (
                  <button
                    disabled={busy}
                    className="quiet danger"
                    onClick={() => {
                      if (
                        confirm(
                          "¿Desactivar a " +
                            u.username +
                            " y revocar su acceso?",
                        )
                      )
                        void run(async () => {
                          await api(
                            "/admin/users/" + u.id + "/deactivate",
                            "POST",
                          );
                        });
                    }}
                  >
                    Desactivar
                  </button>
                )}
              </div>
            ))}
          </div>
        </section>
        <IntegrationManager api={api} onError={onError} />
        <section className="panel">
          <h2>Actividad administrativa</h2>
          <p className="muted">Últimos 100 registros · Horarios UTC</p>
          {audit.map((x) => (
            <div className="audit" key={x.id}>
              <span>{x.action}</span>
              <small>
                {new Date(x.created_at * 1000)
                  .toISOString()
                  .replace("T", " ")
                  .slice(0, 19)}
              </small>
            </div>
          ))}
        </section>
      </div>
    </>
  );
}

function Recovery({
  onError,
  onCodes,
}: {
  onError: (s: string) => void;
  onCodes: (c: string[]) => void;
}) {
  const [busy, setBusy] = useState(false);
  return (
    <section className="panel">
      <h1>Mi acceso</h1>
      <h2>Renovar códigos de recuperación</h2>
      <p className="muted">Los códigos anteriores dejarán de funcionar.</p>
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          const form = e.currentTarget,
            f = new FormData(form);
          setBusy(true);
          onError("");
          try {
            const x = await api("/auth/recovery-codes", "POST", {
              password: f.get("password"),
              code: f.get("code"),
            });
            onCodes(x.recovery_codes);
            form.reset();
          } catch (error) {
            onError((error as Error).message);
          } finally {
            setBusy(false);
          }
        }}
      >
        <label>
          Contraseña actual
          <input
            name="password"
            type="password"
            autoComplete="current-password"
            required
          />
        </label>
        <label>
          Código MFA o de recuperación
          <input name="code" autoComplete="one-time-code" required />
        </label>
        <button disabled={busy}>Renovar códigos</button>
      </form>
      <TelegramLink onError={onError} />
    </section>
  );
}

// Proves that a Telegram account is this user, so verdicts given in the NOC group are recorded under their name.
function TelegramLink({ onError }: { onError: (s: string) => void }) {
  const [status, setStatus] = useState<any>(null);
  const [code, setCode] = useState<any>(null);
  const load = () => api("/me/telegram").then(setStatus).catch((e) => onError((e as Error).message));
  useEffect(() => { void load(); }, []);
  if (!status?.configured) return null;
  return <>
    <h2>Telegram</h2>
    <p className="muted">Vincula tu Telegram para recibir los casos en el grupo del NOC y valorar los diagnósticos desde allí con tu nombre.</p>
    <p>{status.linked ? "Tu cuenta de Telegram está vinculada." : "Tu cuenta de Telegram no está vinculada."}</p>
    {code && <p className="notice">Envía al bot, en un chat privado: <strong className="mono">/vincular {code.code}</strong> · vence en {Math.round(code.expires_in / 60)} min.</p>}
    <button className="secondary" onClick={() => api("/me/telegram", "POST").then(setCode).catch((e) => onError((e as Error).message))}>
      {status.linked ? "Vincular otra cuenta" : "Obtener código"}</button>{" "}
    {status.linked && <button className="quiet" onClick={() => api("/me/telegram", "DELETE").then(() => { setCode(null); void load(); }).catch((e) => onError((e as Error).message))}>Desvincular</button>}
  </>;
}

createRoot(document.getElementById("root")!).render(<App />);
