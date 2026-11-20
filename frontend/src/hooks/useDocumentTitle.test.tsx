import { describe, expect, it } from "vitest";
import { renderHook } from "@testing-library/react";
import { APP_NAME, documentTitle, useDocumentTitle } from "./useDocumentTitle";

describe("useDocumentTitle", () => {
  it("sets the tab title to '<title> · Quantfolio'", () => {
    renderHook(() => useDocumentTitle("Portfolio — Holdings"));

    expect(document.title).toBe("Portfolio — Holdings · Quantfolio");
  });

  it("follows the title as the route changes", () => {
    const { rerender } = renderHook(({ title }) => useDocumentTitle(title), { initialProps: { title: "Home" } });
    expect(document.title).toBe("Home · Quantfolio");

    rerender({ title: "Decide" });

    expect(document.title).toBe("Decide · Quantfolio");
  });

  it("falls back to the bare app name when a route has no title", () => {
    renderHook(() => useDocumentTitle(""));

    expect(document.title).toBe(APP_NAME);
    expect(documentTitle(undefined)).toBe(APP_NAME);
  });
});
