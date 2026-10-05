import { useEffect } from "react";

export const APP_NAME = "Quantfolio";

/** "Portfolio · Quantfolio"; the bare app name when a route has no title. */
export function documentTitle(title?: string | null): string {
  return title ? `${title} · ${APP_NAME}` : APP_NAME;
}

/** Keeps the browser tab / history entry title in step with the current route. */
export function useDocumentTitle(title?: string | null): void {
  useEffect(() => {
    document.title = documentTitle(title);
  }, [title]);
}
