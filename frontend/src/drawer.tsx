import { ReactNode, useEffect, useRef, useState } from "react";

/** A detail opened beside its list: the list stays in place; Esc or the backdrop closes it. `link` is its #hash. */
export function Drawer({ label, link, onClose, children }: { label: string; link?: string; onClose: () => void; children: ReactNode }) {
  const panel = useRef<HTMLDivElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  const [copied, setCopied] = useState(false);
  // Once per opening: refreshes of the list behind must not move the focus out of a form being filled.
  useEffect(() => {
    panel.current?.focus();
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") close.current();
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, []);
  async function copy() {
    try {
      await navigator.clipboard.writeText(window.location.origin + "/" + link);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      /* Clipboard unavailable: the link is still in the address bar of whoever opens it. */
    }
  }
  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} aria-hidden="true" />
      <div className="drawer" role="dialog" aria-label={label} tabIndex={-1} ref={panel}>
        <div className="drawer-bar">
          <span>{label}</span>
          <span>
            {link && (
              <button className="quiet" onClick={() => void copy()}>
                {copied ? "Enlace copiado" : "Copiar enlace"}
              </button>
            )}
            <button className="quiet" onClick={onClose} aria-label="Cerrar panel">
              ✕
            </button>
          </span>
        </div>
        {children}
      </div>
    </>
  );
}
