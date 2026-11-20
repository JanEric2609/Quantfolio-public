import "@testing-library/jest-dom";
import { vi, afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

// rule §5.4: use vi.importActual INSIDE the factory; no top-level `await import()`
vi.mock("lucide-react", async () => await vi.importActual<typeof import("lucide-react")>("lucide-react"));

// fetch + jsdom shims
global.fetch = vi.fn(async () => new Response(JSON.stringify({}), { status: 200 })) as unknown as typeof fetch;
window.HTMLElement.prototype.scrollIntoView = vi.fn();
window.matchMedia = window.matchMedia ?? ((q: string) => ({
  matches: false, media: q, onchange: null, addListener: vi.fn(), removeListener: vi.fn(),
  addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
} as unknown as MediaQueryList));

class _IO { observe = vi.fn(); unobserve = vi.fn(); disconnect = vi.fn(); }
(global as unknown as { IntersectionObserver: typeof _IO }).IntersectionObserver = _IO;
class _RO { observe = vi.fn(); unobserve = vi.fn(); disconnect = vi.fn(); }
(global as unknown as { ResizeObserver: typeof _RO }).ResizeObserver = _RO;

afterEach(() => cleanup());
