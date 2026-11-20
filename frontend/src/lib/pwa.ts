/**
 * Name of the service worker's runtime cache for read-only plan / wealth /
 * holdings responses. Used by `runtimeCaching` in vite.config.ts.
 */
export const API_CACHE_NAME = "api-read-cache";

/**
 * The only API responses the service worker may keep: GET plan, wealth and
 * holdings. It is an allow-list on purpose: `/api/auth*`, `/api/setup*`,
 * everything that writes and every other read are never cached, so a stale
 * or foreign response can never stand in for a login, a decision or a quote.
 * Matches the path (with or without a query string) and nothing below it.
 */
export const API_READ_CACHE_PATTERN = /\/api\/(?:plan\/month|portfolio\/(?:wealth|holdings))(?:\?|$)/;

/**
 * Drops the cached API responses. Called on logout: the cache is per browser
 * origin, not per user, so the next person to sign in must not be served the
 * previous session's figures while offline.
 */
export async function clearApiCache(): Promise<void> {
  try {
    if (typeof caches !== "undefined") await caches.delete(API_CACHE_NAME);
  } catch {
    // Cache storage unavailable (private window, insecure context): nothing to clear.
  }
}
