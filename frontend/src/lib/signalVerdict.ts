import type { SignalVerdict } from "./api";

/** "Too early" is neutral, "failing" red, "passing" green, "not significant" amber. */
export function verdictClass(v: SignalVerdict): string {
  switch (v) {
    case "failing": return "bg-red-500/15 text-red-600 border-red-500/40";
    case "passing": return "bg-green-500/15 text-green-600 border-green-500/40";
    case "not significant": return "bg-amber-500/15 text-amber-600 border-amber-500/40";
    default: return "bg-muted text-muted-foreground border-border";
  }
}
