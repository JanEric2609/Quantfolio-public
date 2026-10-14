// Send browser crashes to the API's error log (POST /api/errors/client), so
// they show up in the Control Center and the daily self-check instead of
// vanishing in a console nobody has open. Never throws, never retries, and
// sends at most MAX_REPORTS distinct errors per page load.

const baseUrl = import.meta.env.VITE_API_BASE_URL ?? "";
const MAX_REPORTS = 10;
const seen = new Set<string>();

// Pages without a session: a report would only earn a 401.
const NO_SESSION_PATHS = ["/login", "/setup"];

function describe(error: unknown): { kind: string; message: string; stack?: string } {
  if (error instanceof Error) {
    return { kind: error.name || "Error", message: error.message, stack: error.stack };
  }
  if (typeof error === "string") return { kind: "Error", message: error };
  try {
    return { kind: "NonError", message: JSON.stringify(error) ?? String(error) };
  } catch {
    return { kind: "NonError", message: String(error) };
  }
}

export function reportError(error: unknown, extra?: string): void {
  try {
    if (typeof window === "undefined" || import.meta.env.MODE === "test") return;
    const path = window.location.pathname;
    if (NO_SESSION_PATHS.some((p) => path.startsWith(p))) return;
    const { kind, message, stack } = describe(error);
    const key = `${kind}|${message}|${path}`;
    if (seen.has(key) || seen.size >= MAX_REPORTS) return;
    seen.add(key);
    const body = JSON.stringify({
      kind: kind.slice(0, 120),
      message: (extra ? `${message} (${extra})` : message).slice(0, 1000),
      location: path.slice(0, 300),
      stack: stack ? stack.slice(-8000) : null,
    });
    void fetch(`${baseUrl}/api/errors/client`, {
      method: "POST",
      credentials: "include",
      keepalive: true,
      headers: { "Content-Type": "application/json" },
      body,
    }).catch(() => undefined);
  } catch {
    // Reporting must never become the error.
  }
}

let installed = false;

// Uncaught errors and unhandled promise rejections outside React's render.
export function installGlobalErrorReporting(): void {
  if (installed || typeof window === "undefined") return;
  installed = true;
  window.addEventListener("error", (event) => {
    // A failed <img>/<script> load has no error object; it is not a crash.
    if (event.error) reportError(event.error);
  });
  window.addEventListener("unhandledrejection", (event) => {
    reportError(event.reason, "unhandled promise rejection");
  });
}
