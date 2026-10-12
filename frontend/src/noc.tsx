import { useEffect, useId, useRef, useState } from "react";
import { Qr } from "./qr";

type Api = (path: string, method?: string, body?: unknown) => Promise<any>;

const reportKinds: [string, string][] = [
  ["inicial", "Informar"],
  ["actualizacion", "Actualizar"],
  ["restablecido", "Restablecido"],
  ["escalado", "Escalar a N2"],
];
const reportNames: Record<string, string> = {
  inicial: "Informe",
  actualizacion: "Actualización",
  restablecido: "Restablecido",
  escalado: "Escalado a N2",
};
const channelNames: Record<string, string> = { telegram: "Telegram", whatsapp: "WhatsApp" };
const serviceStates: Record<string, string> = {
  starting: "Iniciando WhatsApp Web…",
  qr: "Esperando el escaneo del QR",
  authenticated: "QR aceptado, cargando la cuenta…",
  ready: "Cuenta vinculada",
  disconnected: "Desconectado; reintentando",
  logged_out: "Sesión cerrada; generando un QR nuevo…",
  auth_failure: "WhatsApp rechazó la sesión; escanea de nuevo",
  failed: "No se pudo iniciar WhatsApp Web; reintentando",
  unreachable: "El servicio de WhatsApp no responde",
};
// Case- and accent-insensitive, for searching names like "Atención NOC" as "atencion noc".
const fold = (s: string) => s.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
const when = (s: number | null, zone: string) =>
  s ? new Date(s * 1000).toLocaleString("es", { timeZone: zone, dateStyle: "short", timeStyle: "short" }) : "—";
const ago = (s: number) => {
  const m = Math.max(0, Math.round((Date.now() / 1000 - s) / 60));
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
};

function useRunner(onError: (s: string) => void, after: () => Promise<void>) {
  const [busy, setBusy] = useState(false);
  async function run(fn: () => Promise<void>) {
    setBusy(true);
    onError("");
    try {
      await fn();
      await after();
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return { busy, run };
}

/** Level-1 communication of one case: its guide, the reports sent to the NOC group and the next one to send. */
export function CaseCommunication({ api, caseId, zone, onError }: { api: Api; caseId: string; zone: string; onError: (s: string) => void }) {
  const [data, setData] = useState<any>(null),
    [kind, setKind] = useState(""),
    [text, setText] = useState("");
  const load = async () => setData(await api(`/cases/${caseId}/communication`));
  const { busy, run } = useRunner(onError, load);
  useEffect(() => {
    setKind("");
    load().catch((e) => onError(e.message));
  }, [caseId]);
  if (!data) return null;
  const s = data.status,
    g = data.guide;
  return (
    <section className="noc-communication">
      <h3>Comunicación al NOC · {g.title}</h3>
      <p>
        {s.reported_at ? (
          <span className="badge">Informado {when(s.reported_at, zone)}</span>
        ) : s.overdue ? (
          <span className="badge probe-status critical">Sin informar · plazo vencido</span>
        ) : (
          <span className="badge">Sin informar</span>
        )}{" "}
        {s.escalated_at ? (
          <span className="badge">Escalado a N2 {when(s.escalated_at, zone)}</span>
        ) : s.escalate_due ? (
          <span className="badge probe-status warning">Corresponde escalar a N2</span>
        ) : null}
      </p>
      <dl className="event-detail">
        <dt>Verificar</dt>
        <dd>
          <ol>{g.verify.map((v: string) => <li key={v}>{v}</li>)}</ol>
        </dd>
        <dt>Escalamiento a N2</dt>
        <dd>
          {g.escalate}
          {g.escalate_minutes != null && <small> · a los {g.escalate_minutes} min</small>}
        </dd>
        <dt>Canales</dt>
        <dd>
          {data.channels.length
            ? data.channels.map((c: string) => channelNames[c] || c).join(" y ")
            : "Ninguno configurado: el informe queda registrado en ODIN"}
        </dd>
      </dl>
      <div className="probe-actions">
        {reportKinds.map(([k, label]) => (
          <button
            key={k}
            className={k === kind ? "" : "secondary"}
            onClick={() => {
              setKind(k);
              setText(data.drafts[k]);
            }}
          >
            {label}
          </button>
        ))}
      </div>
      {kind && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void run(async () => {
              await api(`/cases/${caseId}/reports`, "POST", { kind, text });
              setKind("");
            });
          }}
        >
          <label>
            {reportNames[kind]} para el grupo del NOC
            <textarea required maxLength={2000} rows={4} value={text} onChange={(e) => setText(e.target.value)} />
          </label>
          {kind === "escalado" && <small>Completa el motivo del escalamiento.</small>}
          <div className="probe-actions">
            <button disabled={busy || !text.trim()}>Enviar</button>
            <button type="button" className="quiet" onClick={() => setKind("")}>
              Cancelar
            </button>
          </div>
        </form>
      )}
      {data.reports.length > 0 && (
        <ol className="noc-reports">
          {data.reports.map((r: any) => (
            <li key={r.id}>
              <strong>{reportNames[r.kind]}</strong> · {r.author} · {when(r.created_at, zone)}
              <p>{r.text}</p>
              <small>
                {Object.entries(r.deliveries || {}).length
                  ? Object.entries(r.deliveries).map(([c, d]: [string, any]) =>
                      `${channelNames[c] || c}: ${d.status === "sent" ? "enviado" : d.status === "pending" ? "en cola" : "falló (" + d.error + ")"}`,
                    ).join(" · ")
                  : "Solo registrado en ODIN"}
              </small>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

type Group = { id: string; name: string; participants: number };

/** One field to search the account's groups by name and pick one (a WhatsApp account can have hundreds). */
function GroupPicker({ groups, value, onChange }: { groups: Group[]; value: string; onChange: (id: string) => void }) {
  const id = useId(),
    input = useRef<HTMLInputElement>(null),
    [open, setOpen] = useState(false),
    [query, setQuery] = useState(""),
    [active, setActive] = useState(-1);
  const selected = groups.find((g) => g.id === value);
  const items = groups.filter((g) => fold(g.name).includes(fold(query.trim())));
  function choose(g: Group) {
    onChange(g.id);
    setQuery("");
    setOpen(false);
    input.current?.focus();
  }
  return (
    <div className="device-select"
      onBlur={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node)) setOpen(false); }}>
      <label htmlFor={id}>Grupo del NOC</label>
      <div className="device-combobox">
        <input id={id} ref={input} role="combobox" aria-expanded={open} aria-controls={id + "-options"}
          aria-autocomplete="list" aria-activedescendant={open && items[active] ? id + "-option-" + active : undefined}
          autoComplete="off" required={!selected} maxLength={100}
          placeholder={open ? "Buscar grupo…" : `Elige uno de los ${groups.length} grupos de la cuenta`}
          value={open ? query : selected ? selected.name : ""}
          onClick={() => { if (!open) { setOpen(true); setQuery(""); setActive(-1); } }}
          onChange={(e) => { setQuery(e.target.value); setOpen(true); setActive(0); }}
          onKeyDown={(e) => {
            if (e.key === "Escape") { e.preventDefault(); setOpen(false); }
            if (e.key === "ArrowDown" || e.key === "ArrowUp") {
              e.preventDefault(); setOpen(true);
              setActive((a) => Math.max(0, Math.min(items.length - 1, a + (e.key === "ArrowDown" ? 1 : -1))));
            }
            if (e.key === "Enter" && open) { e.preventDefault(); if (items[active]) choose(items[active]); }
          }} />
        <button type="button" className="device-toggle" aria-label="Abrir la lista de grupos"
          onClick={() => { setOpen((o) => !o); setQuery(""); setActive(-1); input.current?.focus(); }}>▾</button>
        {open && (
          <div className="device-dropdown">
            <div role="listbox" id={id + "-options"} aria-label="Grupos de la cuenta">
              {items.map((g, i) => (
                <button type="button" role="option" tabIndex={-1} id={id + "-option-" + i} key={g.id}
                  aria-selected={g.id === value} className={active === i ? "active" : ""}
                  onMouseDown={(e) => e.preventDefault()} onClick={() => choose(g)}>
                  <strong>{g.name}</strong>
                  <small> · {g.participants} participantes</small>
                </button>
              ))}
            </div>
            {!items.length && <small role="status">Ningún grupo coincide.</small>}
          </div>
        )}
      </div>
    </div>
  );
}

function WhatsApp({ api, onError }: { api: Api; onError: (s: string) => void }) {
  const [data, setData] = useState<any>(null),
    [group, setGroup] = useState("");
  const load = async () => {
    const d = await api("/noc/whatsapp");
    setData(d);
    setGroup((g) => g || d.group_id || "");
  };
  const { busy, run } = useRunner(onError, load);
  useEffect(() => {
    load().catch((e) => onError(e.message));
    // Pairing changes on WhatsApp's side: follow it until the account is ready.
    const timer = setInterval(() => load().catch(() => undefined), 5000);
    return () => clearInterval(timer);
  }, []);
  if (!data) return <p className="muted">Consultando WhatsApp…</p>;
  const service = data.service || {};
  return (
    <section className="panel">
      <h2>WhatsApp · grupo del NOC</h2>
      <p>
        {serviceStates[service.status] || service.status}
        {service.me && <> · {service.me.name || "cuenta"} (+{service.me.number})</>}
        {service.error && <small> · {service.error}</small>}
      </p>
      {service.status === "qr" && service.qr && (
        <>
          <Qr text={service.qr} label="Código QR para vincular WhatsApp" />
          <small>
            En el teléfono: WhatsApp → Dispositivos vinculados → Vincular un dispositivo. Usa un número dedicado a ODIN:
            WhatsApp Web no es una API oficial.
          </small>
        </>
      )}
      {service.status === "ready" && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void run(async () => {
              await api("/noc/whatsapp", "PUT", { group_id: group });
            });
          }}
        >
          <GroupPicker groups={data.groups || []} value={group} onChange={setGroup} />
          <button disabled={busy || !group || group === data.group_id}>Usar este grupo</button>
        </form>
      )}
      {data.group_name && (
        <p>
          Publica en <strong>{data.group_name}</strong>
          {data.paused && " · en pausa"}
          {data.last_error && <small> · último error: {data.last_error}</small>}
        </p>
      )}
      <div className="probe-actions">
        {data.group_id && (
          <button className="secondary" disabled={busy} onClick={() => void run(async () => {
            await api("/noc/whatsapp/pause", "POST", { paused: !data.paused });
          })}>
            {data.paused ? "Reanudar" : "Pausar"}
          </button>
        )}
        {service.status === "ready" && (
          <button className="quiet" disabled={busy} onClick={() => {
            if (confirm("¿Desvincular la cuenta de WhatsApp? Habrá que escanear un QR nuevo."))
              void run(async () => {
                await api("/noc/whatsapp/logout", "POST");
              });
          }}>
            Desvincular cuenta
          </button>
        )}
      </div>
    </section>
  );
}

function Settings({ api, onError }: { api: Api; onError: (s: string) => void }) {
  const [data, setData] = useState<any>(null),
    [form, setForm] = useState<any>(null);
  const load = async () => {
    const d = await api("/noc/settings");
    setData(d);
    setForm({ report_minutes: d.report_minutes, shifts: d.shifts });
  };
  const { busy, run } = useRunner(onError, load);
  useEffect(() => {
    load().catch((e) => onError(e.message));
  }, []);
  if (!form) return null;
  const shift = (i: number, key: string, value: string) =>
    setForm({ ...form, shifts: form.shifts.map((s: any, n: number) => (n === i ? { ...s, [key]: value } : s)) });
  return (
    <section className="panel">
      <h2>Plazo y turnos</h2>
      <p className="muted">
        Telegram: {data.telegram.group ? "grupo vinculado (/grupo)" : "sin grupo; vincúlalo con /grupo desde el grupo"}
        {data.telegram.paused && " · en pausa"}. Los informes salen por: {data.channels.map((c: string) => channelNames[c]).join(" y ") || "ningún canal"}.
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void run(async () => {
            await api("/noc/settings", "PUT", form);
          });
        }}
      >
        <label>
          Minutos para informar una caída crítica o de acceso
          <input type="number" min={1} max={240} required value={form.report_minutes}
            onChange={(e) => setForm({ ...form, report_minutes: Number(e.target.value) })} />
        </label>
        {form.shifts.map((s: any, i: number) => (
          <div className="probe-actions" key={i}>
            <label>
              Turno {i + 1} · inicio
              <input type="time" required value={s.start} onChange={(e) => shift(i, "start", e.target.value)} />
            </label>
            <label>
              Fin
              <input type="time" required value={s.end} onChange={(e) => shift(i, "end", e.target.value)} />
            </label>
            {form.shifts.length > 1 && (
              <button type="button" className="quiet" onClick={() => setForm({ ...form, shifts: form.shifts.filter((_: any, n: number) => n !== i) })}>
                Quitar
              </button>
            )}
          </div>
        ))}
        <small>Al terminar cada turno ODIN entrega un resumen al grupo; al empezar, el de las horas sin turno.</small>
        <div className="probe-actions">
          {form.shifts.length < 6 && (
            <button type="button" className="secondary" onClick={() => setForm({ ...form, shifts: [...form.shifts, { start: "20:00", end: "06:00" }] })}>
              Agregar turno
            </button>
          )}
          <button disabled={busy}>Guardar</button>
        </div>
      </form>
    </section>
  );
}

function Guides({ api, onError }: { api: Api; onError: (s: string) => void }) {
  const [items, setItems] = useState<any[]>([]),
    [edit, setEdit] = useState<any>(null);
  const load = async () => setItems(await api("/noc/guides"));
  const { busy, run } = useRunner(onError, load);
  useEffect(() => {
    load().catch((e) => onError(e.message));
  }, []);
  return (
    <section className="panel">
      <h2>Guías para N1</h2>
      <p className="muted">
        Qué verificar, qué informar y cuándo escalar en cada situación. En la plantilla se pueden usar {"{lugar}"}, {"{lectura}"},{" "}
        {"{afectacion}"}, {"{zonas}"}, {"{inicio}"}, {"{duracion}"} y {"{servicio}"}.
      </p>
      <ul className="noc-guides">
        {items.map((g) => (
          <li key={g.situation}>
            <button className="link" onClick={() => setEdit({ ...g, verifyText: g.verify.join("\n") })}>
              {g.title}
            </button>
            <small>{g.custom ? " · ajustada para este ISP" : " · por defecto"}</small>
          </li>
        ))}
      </ul>
      {edit && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void run(async () => {
              await api(`/noc/guides/${edit.situation}`, "PUT", {
                title: edit.title,
                verify: edit.verifyText.split("\n"),
                template: edit.template,
                escalate: edit.escalate,
                escalate_minutes: edit.escalate_minutes === "" || edit.escalate_minutes == null ? null : Number(edit.escalate_minutes),
              });
              setEdit(null);
            });
          }}
        >
          <label>
            Título
            <input required maxLength={120} value={edit.title} onChange={(e) => setEdit({ ...edit, title: e.target.value })} />
          </label>
          <label>
            Qué verificar (una por línea)
            <textarea rows={3} value={edit.verifyText} onChange={(e) => setEdit({ ...edit, verifyText: e.target.value })} />
          </label>
          <label>
            Plantilla del informe
            <textarea required rows={3} maxLength={1000} value={edit.template} onChange={(e) => setEdit({ ...edit, template: e.target.value })} />
          </label>
          <label>
            Criterio para escalar a N2
            <input required maxLength={500} value={edit.escalate} onChange={(e) => setEdit({ ...edit, escalate: e.target.value })} />
          </label>
          <label>
            Minutos para escalar (vacío: no se marca)
            <input type="number" min={0} max={1440} value={edit.escalate_minutes ?? ""} onChange={(e) => setEdit({ ...edit, escalate_minutes: e.target.value })} />
          </label>
          <div className="probe-actions">
            <button disabled={busy}>Guardar guía</button>
            {edit.custom && (
              <button type="button" className="secondary" disabled={busy} onClick={() => void run(async () => {
                await api(`/noc/guides/${edit.situation}`, "DELETE");
                setEdit(null);
              })}>
                Volver a la guía por defecto
              </button>
            )}
            <button type="button" className="quiet" onClick={() => setEdit(null)}>
              Cancelar
            </button>
          </div>
        </form>
      )}
    </section>
  );
}

/** Administration of how the NOC communicates: WhatsApp, report deadline and shifts, guides. */
export function NocCommunication({ api, onError }: { api: Api; onError: (s: string) => void }) {
  return (
    <>
      <h1>Comunicación NOC</h1>
      <div className="admin-grid">
        <WhatsApp api={api} onError={onError} />
        <Settings api={api} onError={onError} />
        <Guides api={api} onError={onError} />
      </div>
    </>
  );
}

/** The last shift handover and how the current shift goes so far. */
export function ShiftHandover({ api, zone, latest }: { api: Api; zone: string; latest: any }) {
  const [current, setCurrent] = useState<any>(null);
  return (
    <details className="panel noc-shift" onToggle={(e) => {
      if ((e.target as HTMLDetailsElement).open) api("/noc/shifts?limit=1").then((d) => setCurrent(d.current)).catch(() => undefined);
    }}>
      <summary>
        Entrega de turno
        {latest && <small> · última: {latest.kind === "cierre" ? "cierre" : "apertura"} {when(latest.ends_at, zone)} (hace {ago(latest.ends_at)})</small>}
      </summary>
      {current && (
        <>
          <h3>Turno en curso · desde {when(current.starts_at, zone)} hasta {when(current.ends_at, zone)}</h3>
          <pre className="noc-shift-text">{current.text}</pre>
        </>
      )}
      {latest ? (
        <>
          <h3>{latest.kind === "cierre" ? "Último cierre" : "Última apertura"} · {when(latest.starts_at, zone)} – {when(latest.ends_at, zone)}</h3>
          <pre className="noc-shift-text">{latest.text}</pre>
        </>
      ) : (
        <p className="muted">Todavía no hay entregas de turno.</p>
      )}
    </details>
  );
}
