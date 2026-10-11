import { useState, useEffect, useRef, useId, FormEvent } from "react";
import { Paged } from "./pager";
import type { ChatCase } from "./chat";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import "./inventory.css";

type Api = (path: string, method?: string, body?: unknown) => Promise<any>;
type Pop = {
  id: string;
  name: string;
  description: string;
  latitude: number | null;
  longitude: number | null;
  version: number;
  device_count: number;
};
type Identity = {
  id: string;
  source: string;
  external_id: string;
  name: string;
  addresses: string[];
  details: Record<string, unknown>;
  last_seen: number;
  present: boolean;
  fresh: boolean;
};
type Device = {
  id: string;
  name: string;
  pop_id: string | null;
  version: number;
  sources: Identity[];
  port_count?: number;
};
type Run = {
  id: string;
  source: string;
  mode: string;
  status: string;
  count: number | null;
  error_code: string | null;
  created_at: number;
};
const sourceNames: Record<string, string> = {
  zabbix: "Zabbix",
  librenms: "LibreNMS",
  graylog: "Graylog",
  llama_server: "Modelo local (llama.cpp)",
  deepseek: "DeepSeek (API)",
  telegram: "Telegram · grupos del NOC y de proveedores",
  smartolt: "SmartOLT · OLTs",
};
export const statusNames: Record<string, string> = {
  not_configured: "Sin configurar",
  configured_not_validated: "Sin validar",
  connected: "Conexión comprobada",
  synced: "Sincronizado",
  stale: "Desactualizado",
  error: "Error",
  pending: "Pendiente",
  running: "En curso",
  retrying: "Reintentando",
  done: "Completado",
  failed: "Fallido",
  cancelled: "Cancelado",
};
const errorNames: Record<string, string> = {
  network_not_allowed: "La IP del servidor no está permitida.",
  dns_or_network_invalid: "No se pudo resolver el servidor.",
  token_missing: "Falta el token.",
  connection_failed: "No se pudo conectar. Revisa URL, red y certificado.",
  access_denied: "Acceso denegado. Revisa el token y sus permisos.",
  rpc_access_or_parameters:
    "Zabbix rechazó la consulta. Revisa token, permisos y versión.",
  invalid_response: "Respuesta no compatible.",
  http_error: "El servidor devolvió un error HTTP.",
  redirect_not_allowed: "La URL redirige. Usa la dirección final.",
  incomplete_snapshot:
    "Inventario incompleto; se conservaron los datos anteriores.",
  inventory_changed_retry:
    "El inventario cambió durante la lectura. Se reintentará.",
  inventory_limit: "Se alcanzó el límite de inventario.",
  inventory_timeout: "Se agotó el tiempo de lectura.",
  response_too_large: "La respuesta supera el límite.",
  configuration_changed: "La configuración cambió.",
  unsupported_pagination: "Paginación no compatible con esta respuesta.",
  attempts_exhausted: "Se agotaron los reintentos.",
};
const when = (value: number | null) =>
  value ? new Date(value * 1000).toLocaleString("es") : "—";

function PopMap({
  pops,
  links,
  tileUrl,
  onSelect,
  onPosition,
  picking,
  probeStates,
  onLink,
}: {
  probeStates: Record<string, any>;
  onLink?: (id:string)=>void;
  pops: Pop[];
  links: any[];
  tileUrl: string;
  onSelect: (p: Pop) => void;
  onPosition: (lat: number, lng: number) => void;
  picking: boolean;
}) {
  const node = useRef<HTMLDivElement>(null),
    map = useRef<L.Map | null>(null),
    callbacks = useRef({ onSelect, onPosition, picking });
  callbacks.current = { onSelect, onPosition, picking };
  useEffect(() => {
    if (!node.current) return;
    const m = L.map(node.current).setView([4.6, -74.1], 6);
    map.current = m;
    const tiles = L.tileLayer(tileUrl, {
      maxZoom: 19,
      attribution:
        '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap</a>',
    }).addTo(m);
    m.on("click", (event: L.LeafletMouseEvent) => {
      if (callbacks.current.picking)
        callbacks.current.onPosition(event.latlng.lat, event.latlng.lng);
    });
    const resize = new ResizeObserver(() => m.invalidateSize());
    resize.observe(node.current);
    return () => {
      resize.disconnect();
      tiles.remove();
      m.remove();
      map.current = null;
    };
  }, [tileUrl]);
  const fitted = useRef(false);
  useEffect(() => {
    if (!map.current) return;
    const group = L.layerGroup().addTo(map.current),
      points: L.LatLngTuple[] = [];
    links.forEach(link => {
      if (!link.map_points) return;
      const label = document.createElement("div");
      label.className = "link-tooltip-content";
      for (const side of ["left", "right"]) {
        const endpoint = document.createElement("div");
        endpoint.className = "link-tooltip-endpoint";
        const name = document.createElement("strong");
        name.textContent = link[side + "_name"];
        const port = document.createElement("small");
        port.textContent = link[side + "_port_name"] || "Sin puerto";
        endpoint.append(name, port);
        label.append(endpoint);
      }
      const vlans = document.createElement("div");
      vlans.className = "link-tooltip-vlans";
      vlans.textContent = "VLANs: " + vlanSummary(link.vlans || []);
      label.append(vlans);
      if (link.description) {
        const description = document.createElement("div");
        description.className = "link-tooltip-description";
        description.textContent = link.description;
        label.append(description);
      }
      L.polyline(link.map_points, { color: "#3482c4", weight: 4, opacity: 0.9,
        dashArray: link.left_port_id ? undefined : "6 5", className: "physical-link" })
        .bindTooltip(label, { className: "link-tooltip", sticky: true, opacity: 1 })
        .on("click", event => {L.DomEvent.stopPropagation(event.originalEvent);window.setTimeout(()=>onLink?.(link.id),0);})
        .addTo(group);
    });
    pops.forEach((pop) => {
      if (pop.latitude === null || pop.longitude === null) return;
      const point: L.LatLngTuple = [pop.latitude, pop.longitude];
      points.push(point);
      const label = document.createElement("span");
      const status = probeStates[pop.id];
      label.textContent = pop.name + (status ? ` · ${status.with_data}/${status.targets} destinos evaluados` + (status.severity === 'critical' ? ' · Incidente crítico' : status.severity === 'warning' ? ' · Advertencia' : '') : ' · Sin destinos vigilados');
      L.circleMarker(point, {
        radius: 9,
        color: "#082E54",
        weight: 2,
        fillColor: status?.severity === 'critical' ? '#d84545' : status?.severity === 'warning' ? '#F5A623' : status?.with_data === status?.targets && status?.targets > 0 ? '#3482c4' : '#8697a8',
        dashArray: status && status.with_data < status.targets ? '3 3' : undefined,
        fillOpacity: 1,
      })
        .bindTooltip(label)
        .on("click", (e) => {
          L.DomEvent.stopPropagation(e.originalEvent);
          callbacks.current.onSelect(pop);
        })
        .addTo(group);
    });
    if (points.length && !fitted.current) {
      map.current.fitBounds(L.latLngBounds(points), {
        padding: [40, 40],
        maxZoom: 10,
      });
      fitted.current = true;
    }
    return () => {
      group.remove();
    };
  }, [pops, links, tileUrl, probeStates]);
  return (
    <div className={picking ? "picking" : ""}>
      <div
        className="pop-map"
        ref={node}
        aria-label="Mapa de POPs"
      />
      <p className="muted">Azul: destinos evaluados · Gris: sin cobertura completa · Ámbar/rojo: incidentes de sondeo. El color no representa todos los servicios del POP.</p>
      <small>Cartografía: OpenStreetMap. Si no carga, utiliza la lista.</small>
    </div>
  );
}

function DeviceSelect({ api, name, label, endpoint = "/devices", disabled = false, onSelect }: {
  api: Api; name: string; label: string; endpoint?: string; disabled?: boolean; onSelect?: (id: string | null) => void;
}) {
  const [query, setQuery] = useState("");
  const [items, setItems] = useState<Device[]>([]);
  const [selected, setSelected] = useState<Device | null>(null);
  const [next, setNext] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const root = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const id = useId();
  useEffect(() => {
    function outside(event: PointerEvent) {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    }
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, []);
  useEffect(() => {
    input.current?.setCustomValidity(selected ? "" : "Selecciona una opción de la lista.");
  }, [selected]);
  const generation = useRef(0);
  async function fetchOptions(search: string, after: string | null, current: number) {
    setLoading(true);
    setError("");
    try {
      const result = await api(endpoint + "?" + new URLSearchParams({ q: search, limit: "50", ...(after ? { after } : {}) }));
      if (current !== generation.current) return;
      const options = result.items.map((item: Device) => ({ ...item, sources: item.sources || [] }));
      setItems(previous => after ? [...previous, ...options] : options);
      setNext(result.next_cursor);
    } catch (e) {
      if (current === generation.current) setError((e as Error).message);
    } finally {
      if (current === generation.current) setLoading(false);
    }
  }
  useEffect(() => {
    if (disabled) return;
    const current = ++generation.current;
    setItems([]);
    setActive(-1);
    setNext(null);
    setLoading(true);
    const timer = setTimeout(() => void fetchOptions(query, null, current), 250);
    return () => { clearTimeout(timer); generation.current++; };
  }, [query, endpoint, disabled]);
  function choose(device: Device) {
    setSelected(device);
    onSelect?.(device.id);
    setQuery("");
    setOpen(false);
    input.current?.focus();
  }
  return <div className="device-select" ref={root}
    onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget as Node)) setOpen(false); }}>
    <label htmlFor={id}>{label}</label>
    <input type="hidden" name={name} value={selected?.id || ""} />
    <div className="device-combobox">
      <input id={id} ref={input} role="combobox" aria-expanded={open} aria-controls={id + "-options"}
        aria-autocomplete="list" aria-activedescendant={open && active >= 0 && active < items.length ? id + "-option-" + active : undefined}
        autoComplete="off" disabled={disabled} required={!selected} placeholder={disabled ? "Selecciona el equipo primero" : open ? "Buscar…" : "Seleccionar"} maxLength={200}
        value={open ? query : selected?.name || ""}
        onClick={() => { if (!open) { setOpen(true); setQuery(""); } }}
        onChange={event => { setQuery(event.target.value.slice(0,100)); setOpen(true); setSelected(null); onSelect?.(null); }}
        onKeyDown={event => {
          if (event.key === "Escape") { event.preventDefault(); setOpen(false); }
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault(); setOpen(true);
            setActive(current => Math.max(0, Math.min(items.length - 1, current + (event.key === "ArrowDown" ? 1 : -1))));
          }
          if (event.key === "Enter" && open) {
            event.preventDefault();
            if (items[active]) choose(items[active]);
          }
        }} />
      <button type="button" className="device-toggle" disabled={disabled} aria-label={"Abrir " + label.toLowerCase()}
        onClick={() => { setOpen(value => !value); setQuery(""); input.current?.focus(); }}>▾</button>
    </div>
    {selected?.port_count !== undefined && <small className="muted">
      {[...new Set(selected.sources.map(source => sourceNames[source.source] || source.source))].join(" / ")} · {selected.port_count} puertos importados
    </small>}
    {open && <div className="device-dropdown">
      <div role="listbox" id={id + "-options"} aria-label={label}>
        {items.map((device,index) => <button type="button" role="option" tabIndex={-1}
          id={id + "-option-" + index} key={device.id} aria-selected={selected?.id === device.id}
          className={active === index ? "active" : ""} onMouseDown={event => event.preventDefault()}
          onClick={() => choose(device)}>
          <strong>{device.name}</strong>
          {device.sources[0]?.addresses[0] && <small className="mono">{device.sources[0].addresses[0]}</small>}
          {device.port_count !== undefined && <small>{[...new Set(device.sources.map(source => sourceNames[source.source] || source.source))].join(" / ")} · {device.port_count} puertos</small>}
        </button>)}
      </div>
      {loading && <small role="status">Buscando…</small>}
      {!loading && !error && !items.length && <small role="status">{endpoint.endsWith("/ports") && !query ? "Este equipo no tiene puertos importados. Revisa su registro de LibreNMS." : "Sin resultados."}</small>}
      {error && <small role="alert">{error} <button type="button" className="quiet" onClick={() => void fetchOptions(query, null, generation.current)}>Reintentar</button></small>}
      {next && <button type="button" className="quiet" disabled={loading} onClick={() => void fetchOptions(query, next, generation.current)}>{endpoint.endsWith("/ports") ? "Más puertos" : "Más equipos"}</button>}
    </div>}
  </div>;
}

function LinkEndpoint({ api, side, label }: { api: Api; side: string; label: string }) {
  const [device, setDevice] = useState<string | null>(null);
  return <div className="link-endpoint">
    <DeviceSelect api={api} name={side} label={"Equipo " + label} onSelect={setDevice} />
    <DeviceSelect key={device || "empty"} api={api} name={side + "_port"} label={"Puerto " + label}
      endpoint={device ? "/devices/" + encodeURIComponent(device) + "/ports" : "/devices"} disabled={!device} />
  </div>;
}

function vlanSummary(vlans: number[]) {
  return vlans.length ? vlans.slice(0, 12).join(", ") + (vlans.length > 12 ? ` y ${vlans.length - 12} más` : "") : "Sin especificar";
}

function VlanSelect() {
  const [selected, setSelected] = useState<number[]>([]);
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [limit, setLimit] = useState(100);
  const root = useRef<HTMLDivElement>(null);
  const id = useId();
  useEffect(() => {
    const close = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, []);
  const options = Array.from({ length: 4094 }, (_, i) => i + 1).filter(vlan => String(vlan).includes(query));
  return <div className="device-select vlan-select" ref={root}
    onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget as Node)) setOpen(false); }}
    onKeyDown={event => { if (event.key === "Escape") { setOpen(false); root.current?.querySelector<HTMLButtonElement>(".vlan-trigger")?.focus(); } }}>
    <label id={id}>VLANs del trunk</label>
    <input type="hidden" name="vlans" value={JSON.stringify(selected)} />
    <button type="button" className="vlan-trigger" aria-labelledby={id} aria-expanded={open} aria-controls={id + "-options"}
      onClick={() => setOpen(value => !value)}>{selected.length ? `${selected.length} VLAN seleccionadas` : "Seleccionar VLANs"} ▾</button>
    {selected.length > 0 && <small>{vlanSummary(selected)}</small>}
    {open && <div className="device-dropdown" id={id + "-options"}>
      <input aria-label="Buscar VLAN" type="search" inputMode="numeric" autoFocus maxLength={4} placeholder="Buscar VLAN (1–4094)"
        value={query} onChange={event => { setQuery(event.target.value.replace(/[^0-9]/g, "")); setLimit(100); }} />
      <div className="vlan-options" role="group" aria-label="VLANs disponibles">
        {options.slice(0, limit).map(vlan => <label key={vlan}>
          <input type="checkbox" checked={selected.includes(vlan)} onChange={event => setSelected(values => event.target.checked ? [...values, vlan].sort((a,b) => a-b) : values.filter(value => value !== vlan))} /> VLAN {vlan}
        </label>)}
        {!options.length && <small>Sin resultados.</small>}
      </div>
      {options.length > limit && <button type="button" className="quiet" onClick={() => setLimit(value => value + 100)}>Más VLANs</button>}
      <div className="button-row"><button type="button" className="quiet" onClick={() => setSelected([])}>Limpiar</button>
        <button type="button" onClick={() => setOpen(false)}>Listo</button></div>
    </div>}
  </div>;
}

function sharedSources(left: any, right: any): string[] {
  const rightSources = new Set((right.sources || []).map((source: any) => source.source));
  return [...new Set<string>((left.sources || []).map((source: any) => String(source.source)))].filter(source => rightSources.has(source));
}

function IdentityComparison({ device }: { device: any }) {
  return <div className="identity-comparison">
    <strong>{device.name}</strong>
    <small>{device.pop_name || "Sin POP"} · {device.port_count ?? 0} puertos importados</small>
    {(device.sources || []).map((source: any) => <p key={source.source + source.external_id}>
      <b>{sourceNames[source.source] || source.source}</b> · {source.name}<br />
      <small><span className="mono">{source.addresses.join(", ") || "Sin IP"}</span> · ID {source.external_id}</small>
    </p>)}
  </div>;
}

function ManualReconciliation({ api, onDone, onError }: {
  api: Api; onDone: () => Promise<void>; onError: (message: string) => void;
}) {
  const [leftId, setLeftId] = useState<string | null>(null);
  const [rightId, setRightId] = useState<string | null>(null);
  const [pair, setPair] = useState<any[] | null>(null);
  const [keep, setKeep] = useState("left");
  const [busy, setBusy] = useState(false);
  const [version, setVersion] = useState(0);
  useEffect(() => {
    let active = true;
    setPair(null);
    if (leftId && rightId && leftId !== rightId) {
      Promise.all([api("/devices/" + leftId), api("/devices/" + rightId)])
        .then(devices => { if (active) setPair(devices); })
        .catch(error => { if (active) onError(error.message); });
    }
    return () => { active = false; };
  }, [leftId, rightId]);
  return <details className="manual-reconciliation">
    <summary>Unir equipos manualmente</summary>
    <form onSubmit={async event => {
      event.preventDefault();
      if (!pair) return;
      setBusy(true); onError("");
      try {
        await api("/inventory/reconciliation/manual", "POST", {
          left_id: pair[0].id, right_id: pair[1].id, keep_id: pair[keep === "left" ? 0 : 1].id,
          left_version: pair[0].version, right_version: pair[1].version,
          allow_same_source: sharedSources(pair[0], pair[1]).length > 0,
        });
        setLeftId(null); setRightId(null); setPair(null); setVersion(value => value + 1);
        await onDone();
      } catch (error) { onError((error as Error).message); }
      finally { setBusy(false); }
    }}>
      <p>Selecciona dos registros del mismo equipo.</p>
      <div className="reconciliation-pair">
        <DeviceSelect key={"manual-left-" + version} api={api} name="manual_left" label="Primer registro" onSelect={setLeftId} disabled={busy} />
        <DeviceSelect key={"manual-right-" + version} api={api} name="manual_right" label="Segundo registro" onSelect={setRightId} disabled={busy} />
      </div>
      {leftId && leftId === rightId && <p role="alert">Selecciona dos registros distintos.</p>}
      {pair && <>
        <div className="reconciliation-pair">{pair.map(device => <IdentityComparison key={device.id} device={device} />)}</div>
        <label>Nombre que conservar
          <select aria-label="Nombre que conservar" value={keep} onChange={event => setKeep(event.target.value)} disabled={busy}>
            <option value="left">{pair[0].name}</option><option value="right">{pair[1].name}</option>
          </select>
        </label>
        {sharedSources(pair[0],pair[1]).length > 0 && <p>Hay varios registros de {sharedSources(pair[0],pair[1]).map(source => sourceNames[source] || source).join(", ")}. Se conservarán todos sus IDs de origen.</p>}
        <label className="checkbox-label"><input key={leftId + ":" + rightId} type="checkbox" required /> Confirmo que son el mismo equipo físico</label>
      </>}
      <button disabled={busy || !pair}>Unir equipos</button>
    </form>
  </details>;
}

export function Inventory({
  api,
  admin,
  onError,
  onMonitor,
  onAsk,
}: {
  onMonitor?: (focus:{popId?:string,linkId?:string})=>void;
  onAsk?: (c: ChatCase) => void;
  api: Api;
  admin: boolean;
  onError: (s: string) => void;
}) {
  const [pops, setPops] = useState<Pop[]>([]),
    [devices, setDevices] = useState<Device[]>([]),
    [candidates, setCandidates] = useState<any[]>([]),
    [links, setLinks] = useState<any[]>([]),
    [runs, setRuns] = useState<Run[]>([]);
  const [probeStates, setProbeStates] = useState<Record<string, any>>({});
  useEffect(() => {
    let active = true;
    const poll = () => api("/probe-overview").then(x => { if(active) setProbeStates(x.pops); }).catch(() => {if(active) setProbeStates({});});
    void poll(); const timer = setInterval(poll, 15000);
    return () => { active = false; clearInterval(timer); };
  }, []);
  const [sameSourceConfirmed, setSameSourceConfirmed] = useState<Record<string,boolean>>({});
  const [keepIds, setKeepIds] = useState<Record<string,string>>({});
  const [linkFormVersion, setLinkFormVersion] = useState(0);
  const [tileUrl, setTileUrl] = useState(""),
    [view, setView] = useState("map"),
    [tab, setTab] = useState("devices"),
    [busy, setBusy] = useState(false),
    [loaded, setLoaded] = useState(false);
  const [selected, setSelected] = useState<Pop | null>(null),
    [editing, setEditing] = useState(false),
    [picking, setPicking] = useState(false),
    [name, setName] = useState(""),
    [description, setDescription] = useState(""),
    [lat, setLat] = useState(""),
    [lng, setLng] = useState("");
  const [filter, setFilter] = useState(""),
    [q, setQ] = useState(""),
    [next, setNext] = useState<string | null>(null),
    [detail, setDetail] = useState<any>(null);
  const query = () =>
    "?" +
    new URLSearchParams({
      ...(filter === "unassigned"
        ? { unassigned: "true" }
        : filter
          ? { pop_id: filter }
          : {}),
      ...(q ? { q } : {}),
    }).toString();
  async function load() {
    const [p, d, c, l, r] = await Promise.all([
      api("/pops"),
      api("/devices" + query()),
      api("/inventory/reconciliation"),
      api("/inventory/links"),
      api("/inventory/runs"),
    ]);
    setPops(p);
    setDevices(d.items);
    setNext(d.next_cursor);
    setCandidates(c);
    setLinks(l);
    setRuns(r);
    setLoaded(true);
  }
  useEffect(() => {
    let active = true;
    api("/inventory/map-config")
      .then((x) => {
        if (active) setTileUrl(x.tile_url);
      })
      .catch((e) => onError(e.message));
    return () => {
      active = false;
    };
  }, []);
  useEffect(() => {
    load().catch((e) => onError(e.message));
  }, [filter, q]);
  useEffect(() => {
    const timer = setInterval(
      () =>
        api("/inventory/runs")
          .then(setRuns)
          .catch(() => {}),
      5000,
    );
    return () => clearInterval(timer);
  }, []);
  async function action(fn: () => Promise<void>, reload = true) {
    setBusy(true);
    onError("");
    try {
      await fn();
      if (reload) await load();
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  function edit(pop: Pop | null) {
    setSelected(pop);
    setName(pop?.name || "");
    setDescription(pop?.description || "");
    setLat(pop?.latitude?.toString() || "");
    setLng(pop?.longitude?.toString() || "");
    setEditing(true);
    setPicking(false);
  }
  async function savePop(e: FormEvent) {
    e.preventDefault();
    await action(async () => {
      const saved = await api(
        selected ? "/pops/" + selected.id : "/pops",
        selected ? "PUT" : "POST",
        {
          name,
          description,
          latitude: lat === "" ? null : Number(lat),
          longitude: lng === "" ? null : Number(lng),
          version: selected?.version,
        },
      );
      setSelected(saved);
      setEditing(false);
      setPicking(false);
    });
  }
  async function inspect(id: string) {
    await action(async () => setDetail(await api("/devices/" + id)));
  }
  return (
    <>
      <div className="title-row">
        <h1>Mapa e inventario</h1>
        <div className="button-row">
          <button
            className="secondary"
            disabled={busy}
            onClick={() => void action(async () => {})}
          >
            Actualizar
          </button>
          {admin && <button onClick={() => edit(null)}>Crear POP</button>}
        </div>
      </div>
      <div className="inventory-top">
        <section className="panel">
          <div className="title-row">
            <h2>
              POPs <span className="badge">{pops.length}</span>
            </h2>
            <div className="button-row">
              <button
                className={view === "map" ? "" : "secondary"}
                onClick={() => setView("map")}
              >
                Mapa
              </button>
              <button
                className={view === "list" ? "" : "secondary"}
                onClick={() => setView("list")}
              >
                Lista
              </button>
            </div>
          </div>
          {view === "map" && tileUrl && (
            <PopMap
              onLink={id=>onMonitor?.({linkId:id})}
              probeStates={probeStates}
              pops={pops}
              links={links}
              tileUrl={tileUrl}
              picking={picking}
              onPosition={(a, b) => {
                setLat(a.toFixed(6));
                setLng(b.toFixed(6));
                setPicking(false);
              }}
              onSelect={(p) => {
                setSelected(p);
                setEditing(false);
                setFilter(p.id);
              }}
            />
          )}
          {view === "list" && (
            <div className="pop-list">
              {pops.map((p) => (
                <button
                  className="pop-row"
                  key={p.id}
                  onClick={() => {
                    setSelected(p);
                    setEditing(false);
                    setFilter(p.id);
                  }}
                >
                  <strong>{p.name}</strong>
                  <span>{p.device_count} equipos</span>
                  <small>
                    {p.latitude === null
                      ? "Sin ubicación"
                      : `${p.latitude}, ${p.longitude}`}
                  </small>
                </button>
              ))}
              {loaded && !pops.length && (
                <p className="muted">Sin POPs registrados.</p>
              )}
            </div>
          )}
          {view === "map" && pops.some((p) => p.latitude === null) && (
            <div className="button-row">
              {pops
                .filter((p) => p.latitude === null)
                .map((p) => (
                  <button
                    className="quiet"
                    key={p.id}
                    onClick={() => {
                      setSelected(p);
                      setEditing(false);
                      setFilter(p.id);
                    }}
                  >
                    {p.name} · Sin ubicación
                  </button>
                ))}
            </div>
          )}
        </section>
        <section className="panel pop-editor">
          {editing ? (
            <form onSubmit={savePop}>
              <h2>{selected ? "Editar POP" : "Crear POP"}</h2>
              <label>
                Nombre
                <input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  maxLength={120}
                  required
                />
              </label>
              <label>
                Descripción
                <textarea
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  maxLength={2000}
                />
              </label>
              <div className="field-pair">
                <label>
                  Latitud
                  <input
                    type="number"
                    step="any"
                    min={-90}
                    max={90}
                    value={lat}
                    onChange={(e) => setLat(e.target.value)}
                  />
                </label>
                <label>
                  Longitud
                  <input
                    type="number"
                    step="any"
                    min={-180}
                    max={180}
                    value={lng}
                    onChange={(e) => setLng(e.target.value)}
                  />
                </label>
              </div>
              <button
                type="button"
                className="secondary"
                onClick={() => {
                  setView("map");
                  setPicking(true);
                }}
              >
                {picking
                  ? "Selecciona un punto en el mapa"
                  : "Elegir en el mapa"}
              </button>
              <div className="button-row spaced">
                <button disabled={busy}>Guardar POP</button>
                <button
                  type="button"
                  className="quiet"
                  onClick={() => {
                    setEditing(false);
                    setPicking(false);
                  }}
                >
                  Cancelar
                </button>
              </div>
            </form>
          ) : selected ? (
            <>
              <h2>{selected.name}</h2>
              {onMonitor&&<button className="secondary" onClick={()=>onMonitor({popId:selected.id})}>Ver monitoreo del POP</button>}
              {onAsk&&<button className="secondary" onClick={()=>onAsk({id:selected.id,label:"POP "+selected.name,kind:"pop"})}>Preguntar en el chat</button>}
              <p>{selected.description}</p>
              <p className="muted">
                {selected.latitude === null
                  ? "Sin ubicación"
                  : `${selected.latitude}, ${selected.longitude}`}
              </p>
              <p>
                {pops.find((p) => p.id === selected.id)?.device_count ?? 0}{" "}
                equipos
              </p>
              {admin && (
                <div className="button-row">
                  <button
                    onClick={() =>
                      edit(pops.find((p) => p.id === selected.id) || selected)
                    }
                  >
                    Editar
                  </button>
                  <button
                    className="quiet danger"
                    disabled={busy}
                    onClick={() => {
                      if (confirm("¿Archivar este POP?"))
                        void action(async () => {
                          await api(
                            "/pops/" + selected.id + "/archive",
                            "POST",
                            {
                              version: (
                                pops.find((p) => p.id === selected.id) ||
                                selected
                              ).version,
                            },
                          );
                          setSelected(null);
                          setFilter("");
                        });
                    }}
                  >
                    Archivar
                  </button>
                </div>
              )}
            </>
          ) : (
            <>
              <h2>Selecciona un POP</h2>
              <p className="muted">
                {pops.length
                  ? "Selecciona un punto o utiliza la lista."
                  : "Sin POPs registrados."}
              </p>
            </>
          )}
        </section>
      </div>
      <section className="panel inventory-bottom">
        <div className="tab-row">
          {[
            ["devices", "Equipos"],
            ["candidates", `Coincidencias (${candidates.length})`],
            ["links", "Enlaces"],
            ["runs", "Sincronizaciones"],
          ].map(([key, title]) => (
            <button
              key={key}
              className={tab === key ? "" : "secondary"}
              onClick={() => setTab(key)}
            >
              {title}
            </button>
          ))}
        </div>
        {tab === "devices" && (
          <>
            <div className="inventory-filters">
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  setQ(String(new FormData(e.currentTarget).get("q") || ""));
                }}
              >
                <input
                  name="q"
                  aria-label="Buscar equipo"
                  placeholder="Buscar equipo"
                />
                <button className="secondary">Buscar</button>
              </form>
              <select
                aria-label="Filtrar por POP"
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
              >
                <option value="">Todos los POPs</option>
                <option value="unassigned">Sin asignar</option>
                {pops.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </div>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Equipo</th>
                    <th>Origen e IP</th>
                    <th>POP</th>
                    <th>Inventario</th>
                  </tr>
                </thead>
                <tbody>
                  {devices.map((d) => (
                    <tr key={d.id}>
                      <td>
                        <button
                          className="quiet device-name"
                          onClick={() => void inspect(d.id)}
                        >
                          {d.name}
                        </button>
                      </td>
                      <td>
                        {d.sources.map((s) => (
                          <div key={s.id}>
                            <strong>{sourceNames[s.source]}</strong>{" "}
                            <small>#{s.external_id}</small>
                            <br />
                            <small className="mono">{s.addresses.join(", ") || "—"}</small>
                          </div>
                        ))}
                      </td>
                      <td>
                        {admin ? (
                          <select
                            aria-label={"POP de " + d.name}
                            value={d.pop_id || ""}
                            disabled={busy}
                            onChange={(e) => {
                              const pop = e.target.value;
                              void action(async () => {
                                await api("/devices/" + d.id, "PUT", {
                                  pop_id: pop || null,
                                  version: d.version,
                                });
                              });
                            }}
                          >
                            <option value="">Sin asignar</option>
                            {pops.map((p) => (
                              <option value={p.id} key={p.id}>
                                {p.name}
                              </option>
                            ))}
                          </select>
                        ) : (
                          pops.find((p) => p.id === d.pop_id)?.name ||
                          "Sin asignar"
                        )}
                      </td>
                      <td>
                        {d.sources.map((s) => (
                          <div key={s.id}>
                            <span className="badge">
                              {!s.present
                                ? "No observado"
                                : s.fresh
                                  ? "Actualizado"
                                  : "Desactualizado"}
                            </span>
                            <br />
                            <small>{when(s.last_seen)}</small>
                          </div>
                        ))}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {loaded && !devices.length && (
              <p className="muted">Sin equipos para este filtro.</p>
            )}
            {next && (
              <button
                className="secondary spaced"
                onClick={() =>
                  void action(async () => {
                    const d = await api(
                      "/devices" + query() + "&after=" + next,
                    );
                    setDevices((old) => [...old, ...d.items]);
                    setNext(d.next_cursor);
                  }, false)
                }
              >
                Más equipos
              </button>
            )}
          </>
        )}
        {tab === "candidates" && (
          <>
            {admin && <ManualReconciliation api={api} onDone={load} onError={onError} />}
            {candidates.map((c) => (
              <div className="candidate" key={c.id}>
                <strong>
                  {c.left.name} ↔ {c.right.name}
                </strong>
                <div className="reconciliation-pair"><IdentityComparison device={c.left} /><IdentityComparison device={c.right} /></div>
                <p className="muted">{c.evidence.join(" · ")}</p>
                {admin && <label>Nombre que conservar
                  <select aria-label="Nombre que conservar" value={keepIds[c.id] || c.left.id} onChange={event => setKeepIds(values => ({ ...values, [c.id]: event.target.value }))}>
                    <option value={c.left.id}>{c.left.name}</option><option value={c.right.id}>{c.right.name}</option>
                  </select>
                </label>}
                {admin && sharedSources(c.left,c.right).length > 0 && <label className="checkbox-label">
                  <input type="checkbox" checked={!!sameSourceConfirmed[c.id + ":" + c.left.version + ":" + c.right.version]}
                    onChange={event => setSameSourceConfirmed(values => ({...values, [c.id + ":" + c.left.version + ":" + c.right.version]:event.target.checked}))} />
                  Confirmo que los registros de {sharedSources(c.left,c.right).map(source => sourceNames[source] || source).join(", ")} representan el mismo equipo físico
                </label>}
                {admin && (
                  <div className="button-row">
                    <button
                      disabled={busy || (sharedSources(c.left,c.right).length > 0 && !sameSourceConfirmed[c.id + ":" + c.left.version + ":" + c.right.version])}
                      onClick={() => {
                        if (
                          confirm(
                            "¿Unir las identidades y conservar " +
                              (keepIds[c.id] === c.right.id ? c.right.name : c.left.name) +
                              "?",
                          )
                        )
                          void action(async () => {
                            await api(
                              "/inventory/reconciliation/" + c.id,
                              "POST",
                              {
                                action: "merge",
                                keep_id: keepIds[c.id] || c.left.id,
                                allow_same_source: !!sameSourceConfirmed[c.id + ":" + c.left.version + ":" + c.right.version],
                                left_version: c.left.version,
                                right_version: c.right.version,
                              },
                            );
                          });
                      }}
                    >
                      Unir · conservar {keepIds[c.id] === c.right.id ? c.right.name : c.left.name}
                    </button>
                    <button
                      className="secondary"
                      disabled={busy}
                      onClick={() =>
                        void action(async () => {
                          await api(
                            "/inventory/reconciliation/" + c.id,
                            "POST",
                            {
                              action: "dismiss",
                              left_version: c.left.version,
                              right_version: c.right.version,
                            },
                          );
                        })
                      }
                    >
                      Son equipos distintos
                    </button>
                    <button
                      className="quiet"
                      onClick={() => void inspect(c.left.id)}
                    >
                      Ver primero
                    </button>
                    <button
                      className="quiet"
                      onClick={() => void inspect(c.right.id)}
                    >
                      Ver segundo
                    </button>
                  </div>
                )}
              </div>
            ))}
            {!candidates.length && (
              <p className="muted">Sin coincidencias pendientes.</p>
            )}
          </>
        )}
        {tab === "links" && (
          <>
            {admin && (
              <form
                className="link-form"
                onSubmit={(e) => {
                  e.preventDefault();
                  const form = e.currentTarget,
                    f = new FormData(form);
                  void action(async () => {
                    await api("/inventory/links", "POST", {
                      left_id: f.get("left"),
                      right_id: f.get("right"),
                      left_port_id: f.get("left_port"),
                      right_port_id: f.get("right_port"),
                      vlans: JSON.parse(String(f.get("vlans") || "[]")),
                      description: f.get("description"),
                    });
                    form.reset();
                    setLinkFormVersion(value => value + 1);
                  });
                }}
              >
                <LinkEndpoint key={"left-" + linkFormVersion} api={api} side="left" label="A" />
                <LinkEndpoint key={"right-" + linkFormVersion} api={api} side="right" label="B" />
                <VlanSelect key={"vlans-" + linkFormVersion} />
                <label>
                  Descripción
                  <input name="description" maxLength={300} />
                </label>
                <button disabled={busy}>Registrar enlace</button>
              </form>
            )}
            <small>
              Puertos importados de LibreNMS. VLANs registradas manualmente.
            </small>
            {links.map((l) => (
              <div className="candidate" key={l.id}>
                <strong>
                  {l.left_name || devices.find((d) => d.id === l.left_id)?.name || l.left_id} ↔{" "}
                  {l.right_name || devices.find((d) => d.id === l.right_id)?.name || l.right_id}
                </strong>
                <p>{l.left_port_name || "Sin puerto"} ↔ {l.right_port_name || "Sin puerto"}</p>
                <p>VLANs: {vlanSummary(l.vlans || [])}</p>
                {(l.left_port_present === false || l.right_port_present === false) && <small className="error">Un puerto ya no aparece en el inventario.</small>}
                {l.map_reason && <small>{({ unassigned: "Asigna ambos equipos a un POP para ver la línea.", missing_coordinates: "Completa las coordenadas de ambos POPs para ver la línea.", same_location: "Ambos equipos están en la misma ubicación." } as Record<string,string>)[l.map_reason]}</small>}
                <p>{l.description}</p>
                {admin && (
                  <button
                    className="quiet danger"
                    onClick={() => {
                      if (confirm("¿Archivar este enlace?"))
                        void action(async () => {
                          await api(
                            "/inventory/links/" + l.id + "/archive",
                            "POST",
                          );
                        });
                    }}
                  >
                    Archivar
                  </button>
                )}
              </div>
            ))}
            {!links.length && <p className="muted">Sin enlaces registrados.</p>}
          </>
        )}
        {tab === "runs" && (
          <>
            {runs.map((r) => (
              <div className="candidate" key={r.id}>
                <strong>
                  {sourceNames[r.source]} ·{" "}
                  {r.mode === "test" ? "Conexión" : "Inventario"}
                </strong>
                <span className="badge">
                  {statusNames[r.status] || r.status}
                </span>
                <p className="muted">
                  {when(r.created_at)}
                  {r.count !== null ? ` · ${r.count} equipos` : ""}
                </p>
                {r.error_code && (
                  <p className="error">
                    {errorNames[r.error_code] || r.error_code}
                  </p>
                )}
              </div>
            ))}
            {!runs.length && <p className="muted">Sin sincronizaciones.</p>}
          </>
        )}
      </section>
      {detail && (
        <section className="panel spaced">
          <div className="title-row">
            <h2>{detail.name}</h2>
            <button className="quiet" onClick={() => setDetail(null)}>
              Cerrar detalle
            </button>
          </div>
          <div className="sources">
            {detail.sources.map((s: Identity) => (
              <div key={s.id}>
                <strong>
                  {sourceNames[s.source]} · #{s.external_id}
                </strong>
                <span>{s.name}</span>
                <small className="mono">{s.addresses.join(", ")}</small>
                <small>Ubicación: {String(s.details.location || "—")}</small>
                <small>Modelo: {String(s.details.model || "—")}</small>
                <small>Serie: {String(s.details.serial || "—")}</small>
              </div>
            ))}
          </div>
          <h3>Interfaces</h3>
          <Paged items={detail.interfaces}>{(page) => <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Nombre</th>
                  <th>Tipo</th>
                  <th>Estado del inventario</th>
                </tr>
              </thead>
              <tbody>
                {page.map((i: any) => (
                  <tr key={i.id}>
                    <td>{i.name}</td>
                    <td>
                      {i.kind === "management"
                        ? "Acceso de monitoreo"
                        : "Puerto"}
                    </td>
                    <td>{i.present ? "Observada" : "No observada"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>}</Paged>
          {detail.interfaces_truncated && (
            <p className="muted">Se muestran las primeras 1000 interfaces.</p>
          )}
          <h3>Asignaciones</h3>
          {detail.history.map((h: any, i: number) => (
            <p key={i}>
              <small>
                {when(h.at)} ·{" "}
                {pops.find((p) => p.id === h.from)?.name ||
                  h.from ||
                  "Sin asignar"}{" "}
                →{" "}
                {pops.find((p) => p.id === h.to)?.name || h.to || "Sin asignar"}
              </small>
            </p>
          ))}
          {admin && (
            <button
              className="quiet danger"
              onClick={() => {
                if (confirm("¿Archivar el equipo conservando su historial?"))
                  void action(async () => {
                    await api("/devices/" + detail.id + "/archive", "POST", {
                      version: detail.version,
                    });
                    setDetail(null);
                  });
              }}
            >
              Archivar equipo
            </button>
          )}
        </section>
      )}
    </>
  );
}

export function IntegrationManager({
  api,
  onError,
}: {
  api: Api;
  onError: (s: string) => void;
}) {
  const [rows, setRows] = useState<any[]>([]),
    [kind, setKind] = useState("zabbix"),
    [endpoint, setEndpoint] = useState(""),
    [networks, setNetworks] = useState(""),
    [secret, setSecret] = useState(""),
    [automatic, setAutomatic] = useState(false),
    [model, setModel] = useState(""),
    [busy, setBusy] = useState(false),
    [message, setMessage] = useState(""),
    [runs, setRuns] = useState<Run[]>([]);
  async function refresh() {
    setRows(await api("/admin/integrations"));
    setRuns(await api("/inventory/runs"));
  }
  useEffect(() => {
    refresh().catch((e) => onError(e.message));
    const timer = setInterval(() => {
      refresh().catch(() => {});
    }, 5000);
    return () => clearInterval(timer);
  }, []);
  const row = rows.find((x) => x.kind === kind);
  useEffect(() => {
    setEndpoint(row?.endpoint || "");
    setNetworks((row?.allowed_networks || []).join(", "));
    setAutomatic(row?.auto_sync || false);
    setModel(row?.model || "");
    setSecret("");
    setMessage("");
  }, [kind, row?.version]);
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
  const supported = kind === "zabbix" || kind === "librenms" || kind === "graylog" || kind === "llama_server" || kind === "deepseek" || kind === "telegram" || kind === "smartolt";
  // A model API is reached by public HTTPS address only: no allowed networks to configure.
  const api_model = kind === "deepseek";
  // SmartOLT too: a public service, reached by its *.smartolt.com address only.
  const public_api = api_model || kind === "smartolt";
  // Graylog supplies logs, not inventory: connection test only.
  const inventory = kind === "zabbix" || kind === "librenms";
  const latest = runs.find((x) => x.source === kind);
  return (
    <section className="panel">
      <h2>Fuentes</h2>
      <label>
        Fuente
        <select value={kind} onChange={(e) => setKind(e.target.value)}>
          {Object.entries(sourceNames).map(([key, label]) => (
            <option key={key} value={key}>
              {label}
            </option>
          ))}
        </select>
      </label>
      <span className="badge">
        {statusNames[row?.status || "not_configured"]}
      </span>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void run(async () => {
            await api("/admin/integrations/" + kind, "PUT", {
              endpoint,
              secret: secret || undefined,
              allowed_networks: public_api
                ? undefined
                : networks
                    .split(",")
                    .map((x) => x.trim())
                    .filter(Boolean),
              model: api_model ? model || undefined : undefined,
              auto_sync: automatic,
              version: row?.version,
            });
            setSecret("");
            setMessage("Configuración guardada.");
          });
        }}
      >
        <label>
          URL base
          <input
            type="url"
            value={endpoint}
            onChange={(e) => setEndpoint(e.target.value)}
            required
            placeholder={
              kind === "zabbix"
                ? "https://zabbix.example/api_jsonrpc.php"
                : kind === "graylog"
                  ? "http://graylog:9000"
                  : kind === "llama_server"
                    ? "http://172.18.0.1:8081"
                    : kind === "deepseek"
                      ? "https://api.deepseek.com"
                      : kind === "smartolt"
                        ? "https://tuisp.smartolt.com"
                    : kind === "telegram"
                      ? "https://api.telegram.org"
                      : "https://servidor"
            }
          />
        </label>
        <label>
          Token
          <input
            type="password"
            value={secret}
            onChange={(e) => setSecret(e.target.value)}
            autoComplete="new-password"
            placeholder={
              row?.has_secret
                ? "Guardado · vacío conserva el actual"
                : kind === "llama_server"
                  ? "Clave de la API de llama-server"
                  : kind === "deepseek"
                    ? "API key de DeepSeek"
                    : kind === "smartolt"
                      ? "API key de SmartOLT (X-Token)"
                  : kind === "telegram"
                    ? "Token del bot (de @BotFather)"
                    : "Token de lectura"
            }
          />
        </label>
        {api_model && (
          <>
            <label>
              Modelo
              <input
                value={model}
                onChange={(e) => setModel(e.target.value)}
                placeholder="deepseek-chat"
                maxLength={120}
              />
            </label>
            <p className="notice">
              Lo que analiza el modelo sale de tu red hacia la API de
              DeepSeek: nombres de equipos, IPs, descripciones de puertos,
              extractos de logs y casos. Actívalo en Casos solo si la empresa
              acepta ese envío.
            </p>
          </>
        )}
        {kind === "smartolt" && (
          <p className="notice">
            ODIN solo lee de SmartOLT: no puede reiniciar, deshabilitar ni
            cambiar ninguna ONU u OLT. De las ONUs no se guardan dirección,
            contacto ni credenciales. Usa unas 30 llamadas por hora de las
            1.000 de la cuenta. Actívalo en Monitoreo → OLTs.
          </p>
        )}
        {supported && !public_api && (
          <>
            <label>
              Redes permitidas del servidor (CIDR)
              <input
                value={networks}
                onChange={(e) => setNetworks(e.target.value)}
                placeholder="10.0.0.15/32, 2001:db8::15/128"
                required
              />
            </label>
            <small>
              IP o redes del servidor de esta API. Los destinos fuera de ellas
              se bloquean.
            </small>
            {inventory && (
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={automatic}
                  onChange={(e) => setAutomatic(e.target.checked)}
                />
                Sincronizar cada 15 minutos
              </label>
            )}
          </>
        )}
        <button disabled={busy}>Guardar</button>
      </form>
      {message && <p role="status">{message}</p>}
      {supported ? (
        <>
          <div className="button-row spaced">
            <button
              className="secondary"
              disabled={busy || !row?.has_secret}
              onClick={() =>
                void run(async () => {
                  await api("/admin/integrations/" + kind + "/test", "POST");
                  setMessage("Prueba solicitada.");
                })
              }
            >
              Probar conexión
            </button>
            {inventory && (
              <button
                disabled={busy || !row?.has_secret}
                onClick={() =>
                  void run(async () => {
                    await api("/admin/integrations/" + kind + "/sync", "POST");
                    setMessage("Sincronización solicitada.");
                  })
                }
              >
                Sincronizar
              </button>
            )}
          </div>
          <p className="muted">
            Conexión: {when(row?.checked_at)}
            {inventory && (
              <>
                <br />
                Inventario: {when(row?.synced_at)}
              </>
            )}
          </p>
          {latest && (
            <p>
              <span className="badge">
                {latest.mode === "test" ? "Prueba" : "Sincronización"} ·{" "}
                {statusNames[latest.status]}
              </span>
              {latest.count !== null && ` · ${latest.count} equipos`}
            </p>
          )}
          {row?.last_error && (
            <p role="alert" className="error">
              {errorNames[row.last_error] || row.last_error}
            </p>
          )}
        </>
      ) : (
        <p className="muted">Conexión pendiente de implementación.</p>
      )}
    </section>
  );
}
