import { useSyncExternalStore } from "react";

/**
 * Subscribes to a CSS media query. Returns `false` when `matchMedia` is missing
 * (SSR, very old browsers), so the desktop layout is always the fallback.
 */
export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (notify) => {
      if (typeof window === "undefined" || typeof window.matchMedia !== "function") return () => {};
      const list = window.matchMedia(query);
      list.addEventListener?.("change", notify);
      return () => list.removeEventListener?.("change", notify);
    },
    () => (typeof window !== "undefined" && typeof window.matchMedia === "function" ? window.matchMedia(query).matches : false),
    () => false,
  );
}

/** True below Tailwind's `sm` breakpoint (640 px): phones in portrait. */
export function useIsPhone(): boolean {
  return useMediaQuery("(max-width: 639.98px)");
}
