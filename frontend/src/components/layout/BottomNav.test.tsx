import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { BottomNav } from "./BottomNav";

let pending = 0;
vi.mock("../../hooks/useDecideItems", () => ({ useDecideCount: () => pending }));

function renderNav(path: string, onMore = () => {}) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <BottomNav onMore={onMore} />
    </MemoryRouter>,
  );
  return screen.getByRole("navigation", { name: "Quick navigation" });
}

describe("BottomNav", () => {
  it("has five slots: Plan, Overview, Holdings, Decide, More", () => {
    pending = 0;
    const nav = renderNav("/plan");

    expect(within(nav).getAllByRole("listitem")).toHaveLength(5);
    expect(within(nav).getByRole("link", { name: "Plan" })).toHaveAttribute("href", "/plan");
    expect(within(nav).getByRole("link", { name: "Overview" })).toHaveAttribute("href", "/");
    expect(within(nav).getByRole("link", { name: "Holdings" })).toHaveAttribute("href", "/portfolio/holdings");
    expect(within(nav).getByRole("link", { name: "Decide" })).toHaveAttribute("href", "/decide");
    expect(within(nav).getByRole("button", { name: "More" })).toBeInTheDocument();
  });

  it("marks only the current destination", () => {
    pending = 0;
    const nav = renderNav("/portfolio/accounts");

    expect(within(nav).getByRole("link", { name: "Holdings" })).toHaveAttribute("aria-current", "page");
    expect(within(nav).getAllByRole("link").filter((a) => a.getAttribute("aria-current") === "page")).toHaveLength(1);
  });

  it("treats the detailed overview as Overview", () => {
    pending = 0;
    const nav = renderNav("/dashboard");

    expect(within(nav).getByRole("link", { name: "Overview" })).toHaveAttribute("aria-current", "page");
  });

  it("shows no badge on Decide when nothing waits", () => {
    pending = 0;
    const idle = renderNav("/plan");
    expect(within(idle).getByRole("link", { name: "Decide" })).toHaveTextContent(/^Decide$/);
  });

  it("badges Decide with the pending count", () => {
    pending = 3;
    const nav = renderNav("/plan");

    const decide = within(nav).getByRole("link", { name: /Decide/ });
    expect(decide).toHaveTextContent("3");
    expect(decide).toHaveAccessibleName("Decide, 3 waiting");
  });

  it("caps a large count", () => {
    pending = 140;
    const nav = renderNav("/plan");

    expect(within(nav).getByRole("link", { name: /Decide/ })).toHaveTextContent("99+");
  });

  it("opens the More sheet", () => {
    pending = 0;
    const onMore = vi.fn();
    const nav = renderNav("/plan", onMore);

    fireEvent.click(within(nav).getByRole("button", { name: "More" }));

    expect(onMore).toHaveBeenCalledOnce();
  });
});
