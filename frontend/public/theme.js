// Runs before the application to avoid flashing the wrong theme. No credentials here.
(() => {
  let preference = "system";
  try {
    const saved = localStorage.getItem("odin.theme");
    if (["light", "dark", "system"].includes(saved)) preference = saved;
  } catch (_) {
    /* Storage may be unavailable in private/locked-down browsers. */
  }
  const dark =
    preference === "dark" ||
    (preference === "system" &&
      matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  document.documentElement.dataset.themePreference = preference;
})();
