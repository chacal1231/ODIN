import { useState } from "react";

type Api = (path: string, method?: string, body?: unknown) => Promise<any>;

/** Inventory association of an external source name: suggestions and search, confirmed by an administrator. */
export function AssociationPicker({ api, admin, row, busy, onAssign }: {
  api: Api; admin: boolean; row: any; busy: boolean; onAssign: (deviceId: string | null) => void;
}) {
  const [search, setSearch] = useState("");
  const [found, setFound] = useState<any[]>([]);
  const [error, setError] = useState("");
  if (row.device_id)
    return <>{row.device_name} {admin && <button className="quiet" disabled={busy} onClick={() => onAssign(null)}>Quitar</button>}</>;
  if (!admin) return <>Sin asociar</>;
  const find = async () => {
    try { setFound((await api("/devices?limit=8&q=" + encodeURIComponent(search))).items); setError(""); }
    catch (e) { setError((e as Error).message); }
  };
  return <div className="sender-assign">
    {row.suggestions.map((s: any) => <button key={s.device_id} className="quiet" disabled={busy} onClick={() => onAssign(s.device_id)}>
      Asociar a {s.name} ({s.reasons.map((x: string) => x === "address" ? "misma IP" : "mismo nombre").join(", ")})
    </button>)}
    <input aria-label={"Buscar equipo para " + row.name} placeholder="Buscar equipo" value={search} onChange={(e) => setSearch(e.target.value)} />
    <button className="quiet" disabled={busy} onClick={() => void find()}>Buscar</button>
    {found.map((d) => <button key={d.id} className="quiet" disabled={busy} onClick={() => onAssign(d.id)}>Asociar a {d.name}</button>)}
    {error && <span role="alert">{error}</span>}
  </div>;
}
