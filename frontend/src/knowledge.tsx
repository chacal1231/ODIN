import { FormEvent, useEffect, useState } from "react";

type Api = (path: string, method?: string, body?: unknown) => Promise<any>;
export type KnowledgeDraft = { title: string; body: string; case_kind: string; keywords: string; source_case_id?: string };

const kindNames: Record<string, string> = { "": "Cualquier caso", bgp_session: "Sesiones BGP", interface: "Interfaces", latency: "Latencia" };
const empty: KnowledgeDraft = { title: "", body: "", case_kind: "", keywords: "" };

export function KnowledgePanel({ api, admin, draft, focusId, onDone }: {
  api: Api; admin: boolean; draft: KnowledgeDraft | null; focusId: string | null; onDone: () => void;
}) {
  const [items, setItems] = useState<any[]>([]);
  const [form, setForm] = useState<KnowledgeDraft & { id?: string }>(empty);
  const [editing, setEditing] = useState(false);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");
  const load = () => api("/knowledge").then(setItems).catch((e) => setError((e as Error).message));
  useEffect(() => { void load(); }, [api]);
  useEffect(() => { if (draft) { setForm(draft); setEditing(true); setOpen(true); } }, [draft]);
  useEffect(() => {
    if (!focusId) return;
    setOpen(true);
    window.setTimeout(() => document.getElementById("kn-" + focusId)?.scrollIntoView({ behavior: "smooth", block: "center" }), 50);
  }, [focusId]);
  const save = async (e: FormEvent) => {
    e.preventDefault();
    const body = { title: form.title, body: form.body, case_kind: form.case_kind || null, keywords: form.keywords.split(",").map((k) => k.trim()).filter(Boolean),
      source_case_id: form.source_case_id || null };
    try {
      if (form.id) await api("/knowledge/" + form.id, "PUT", body); else await api("/knowledge", "POST", body);
      setForm(empty); setEditing(false); onDone(); await load();
    } catch (err) { setError((err as Error).message); }
  };
  const archive = async (id: string) => {
    if (!window.confirm("¿Archivar este conocimiento? Deja de usarse en los análisis.")) return;
    try { await api(`/knowledge/${id}/archive`, "POST"); await load(); } catch (err) { setError((err as Error).message); }
  };
  return <section className="panel probe-panel" id="knowledge">
    <div className="title-row"><h2>Conocimiento del NOC ({items.length})</h2>
      <span><button className="quiet" onClick={() => setOpen(!open)}>{open ? "Ocultar" : "Mostrar"}</button>
        {admin && !editing && <button className="quiet" onClick={() => { setForm(empty); setEditing(true); setOpen(true); }}>Añadir</button>}</span></div>
    <p className="muted">Procedimientos y conclusiones validados por el NOC. El analista los usa en los casos y consultas que coinciden con su tipo y sus palabras clave. El texto del modelo nunca se guarda aquí por sí solo.</p>
    {open && <>
      {editing && <form className="knowledge-form" onSubmit={save}>
        <label>Título<input value={form.title} minLength={3} maxLength={200} onChange={(e) => setForm({ ...form, title: e.target.value })} required /></label>
        <label>Contenido<textarea value={form.body} maxLength={5000} rows={5} onChange={(e) => setForm({ ...form, body: e.target.value })} required /></label>
        <div className="event-filters">
          <label>Aplica a<select value={form.case_kind} onChange={(e) => setForm({ ...form, case_kind: e.target.value })}>
            {Object.entries(kindNames).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
          <label>Palabras clave (separadas por comas)<input value={form.keywords} onChange={(e) => setForm({ ...form, keywords: e.target.value })} placeholder="AS 64500, maximum number of prefixes" /></label>
        </div>
        <small className="muted">Sin palabras clave se usa en todos los casos del tipo elegido.</small>
        <div className="button-row"><button>Guardar</button><button type="button" className="quiet" onClick={() => { setEditing(false); setForm(empty); }}>Cancelar</button></div>
      </form>}
      {!items.length && <p>Todavía no hay conocimiento registrado.</p>}
      <ul className="case-impact">{items.map((k) => <li key={k.id} id={"kn-" + k.id} className={focusId === k.id ? "highlight" : ""}>
        <strong>{k.title}</strong> <span className="badge">{kindNames[k.case_kind || ""]}</span>
        {!!k.keywords.length && <small> · {k.keywords.join(", ")}</small>}
        <p className="knowledge-body">{k.body}</p>
        <small className="muted">{k.created_by || "usuario"}</small>
        {admin && <> <button className="quiet" onClick={() => { setForm({ id: k.id, title: k.title, body: k.body, case_kind: k.case_kind || "", keywords: k.keywords.join(", ") }); setEditing(true); }}>Editar</button>
          <button className="quiet" onClick={() => void archive(k.id)}>Archivar</button></>}
      </li>)}</ul>
    </>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
