import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { DeskLayout } from "./DeskLayout";

describe("DeskLayout", () => {
  it("has the one h1 and a tab row with the three desk views, the current one marked", () => {
    render(
      <MemoryRouter initialEntries={["/desk/signals"]}>
        <Routes><Route path="desk" element={<DeskLayout />}><Route path="signals" element={<h2>Signals</h2>} /></Route></Routes>
      </MemoryRouter>,
    );
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    const nav = screen.getByRole("navigation", { name: "Desk sections" });
    expect(Array.from(nav.querySelectorAll("a")).map((a) => a.textContent)).toEqual(["Knock-outs & warrants", "Intraday CFD", "Signals"]);
    expect(nav.querySelector("[aria-current]")?.textContent).toBe("Signals");
  });
});
