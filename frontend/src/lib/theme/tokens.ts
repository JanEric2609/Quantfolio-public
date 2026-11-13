/**
 * Colour tokens for code that cannot use a Tailwind class or a CSS variable
 * (ECharts draws on a canvas). The values come from styles.css, so a chart and
 * the page around it never disagree; the fallback only applies when there is no DOM.
 */
export type ColorToken = "success" | "danger" | "warn" | "info" | "accent" | "text-muted" | "text-secondary" | "border";

const FALLBACK: Record<ColorToken, [number, number, number]> = {
  success: [34, 197, 94],
  danger: [239, 68, 68],
  warn: [234, 179, 8],
  info: [59, 130, 246],
  accent: [250, 128, 1],
  "text-muted": [138, 138, 138],
  "text-secondary": [179, 179, 179],
  border: [38, 38, 38],
};

export function token(name: ColorToken, alpha = 1): string {
  let rgb = FALLBACK[name];
  if (typeof document !== "undefined") {
    const raw = getComputedStyle(document.documentElement).getPropertyValue(`--c-${name}`).trim();
    const parts = raw.split(/\s+/).map(Number);
    if (parts.length === 3 && parts.every((n) => Number.isFinite(n))) rgb = parts as [number, number, number];
  }
  return alpha >= 1 ? `rgb(${rgb.join(",")})` : `rgba(${rgb.join(",")},${alpha})`;
}
