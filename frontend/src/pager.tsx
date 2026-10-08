import { ReactNode, useEffect, useState } from "react";

/** Client-side pages for lists the API returns whole; `focus` opens the page holding that row. */
export function Paged<T>({ items, size = 25, focus, children }: {
  items: T[]; size?: number; focus?: (item: T) => boolean; children: (page: T[]) => ReactNode;
}) {
  const [page, setPage] = useState(0);
  const pages = Math.max(1, Math.ceil(items.length / size));
  const target = focus ? items.findIndex(focus) : -1;
  useEffect(() => { if (target >= 0) setPage(Math.floor(target / size)); }, [target, size]);
  const current = Math.min(page, pages - 1);
  return <>
    {children(items.slice(current * size, (current + 1) * size))}
    {pages > 1 && <div className="pager" role="navigation" aria-label="Paginación">
      <button className="secondary" disabled={current === 0} onClick={() => setPage(current - 1)}>Anterior</button>
      <span>{current * size + 1}–{Math.min(items.length, (current + 1) * size)} de {items.length}</span>
      <button className="secondary" disabled={current === pages - 1} onClick={() => setPage(current + 1)}>Siguiente</button>
    </div>}
  </>;
}
