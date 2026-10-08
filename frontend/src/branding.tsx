import { useEffect, useState } from "react";

type Theme = "light" | "dark" | "system";
export function useTheme() {
  const [theme, setTheme] = useState<Theme>(
    () =>
      (document.documentElement.dataset.themePreference as Theme) || "system",
  );
  useEffect(() => {
    const media = matchMedia("(prefers-color-scheme: dark)");
    const apply = () => {
      const effective =
        theme === "system" ? (media.matches ? "dark" : "light") : theme;
      document.documentElement.dataset.theme = effective;
      document.documentElement.dataset.themePreference = theme;
      document
        .querySelector('meta[name="theme-color"]')
        ?.setAttribute("content", effective === "dark" ? "#081B2D" : "#F3F7FC");
    };
    apply();
    try {
      localStorage.setItem("odin.theme", theme);
    } catch (_) {
      /* Theme still works in memory. */
    }
    media.addEventListener("change", apply);
    const sync = (event: StorageEvent) => {
      if (event.key === "odin.theme")
        setTheme(
          event.newValue === "light" || event.newValue === "dark"
            ? event.newValue
            : "system",
        );
    };
    window.addEventListener("storage", sync);
    return () => {
      media.removeEventListener("change", apply);
      window.removeEventListener("storage", sync);
    };
  }, [theme]);
  return { theme, setTheme };
}
export function ThemePicker({
  theme,
  onChange,
}: {
  theme: Theme;
  onChange: (value: Theme) => void;
}) {
  return (
    <label className="theme-picker">
      <span aria-hidden="true">◐</span>
      <select
        aria-label="Tema de apariencia"
        value={theme}
        onChange={(e) => onChange(e.target.value as Theme)}
      >
        <option value="light">Claro</option>
        <option value="dark">Oscuro</option>
        <option value="system">Sistema</option>
      </select>
    </label>
  );
}
export function Brand({ full = false }: { full?: boolean }) {
  return (
    <div className={full ? "odin-brand full" : "odin-brand compact"}>
      <img src="/odin-eye.svg" alt="Ojo de ODIN con dos cuervos" />
      <div className="brand-wordmark">
        <span className="brand-name">ODIN</span>
      </div>
      {full && (
        <>
          <p className="brand-expansion">
            OMNISCIENT DIGITAL INFRASTRUCTURE NEXUS
          </p>
          <p className="brand-motto">
            “Un ojo en cada ruta, un cuervo en cada nodo”
          </p>
        </>
      )}
    </div>
  );
}
