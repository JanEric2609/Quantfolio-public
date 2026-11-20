import { useLocation, useMatches } from "react-router-dom";
import { titleForPath } from "../../lib/routeManifest";

/**
 * What a route may declare in `handle`:
 * - `title`: the page title (tab title, mobile top bar).
 * - `noPageHeader`: the page renders no `PageHeader`, so the shell supplies the
 *   page's one h1 (visually hidden) and keeps the title visible on desktop too.
 */
export interface RouteHandle {
  title?: string;
  noPageHeader?: boolean;
}

export interface RouteMeta {
  title: string;
  noPageHeader: boolean;
}

/** Deepest declared title wins; a route without one falls back to its manifest entry. */
export function useRouteMeta(): RouteMeta {
  const matches = useMatches();
  const { pathname } = useLocation();
  let title: string | undefined;
  let noPageHeader = false;
  for (let i = matches.length - 1; i >= 0; i--) {
    const handle = matches[i].handle as RouteHandle | undefined;
    if (!title && handle?.title) title = handle.title;
    if (handle?.noPageHeader) noPageHeader = true;
  }
  return { title: title ?? titleForPath(pathname) ?? "", noPageHeader };
}
