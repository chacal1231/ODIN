import qrcode from "qrcode-generator";

// Drawn in the browser: what the QR carries (a TOTP secret, a WhatsApp pairing code) never goes to a QR service.
export function Qr({ text, label }: { text: string; label: string }) {
  const qr = qrcode(0, "M");
  qr.addData(text);
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
      aria-label={label}
      shapeRendering="crispEdges"
    >
      <rect x={-4} y={-4} width={n + 8} height={n + 8} fill="#fff" />
      <path d={d} fill="#000" />
    </svg>
  );
}
