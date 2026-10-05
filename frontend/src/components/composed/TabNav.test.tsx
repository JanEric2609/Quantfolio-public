import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { TabNav } from "./TabNav";

describe("TabNav", () => {
  it("marks the active route tab and labels the nav", () => {
    render(
      <MemoryRouter initialEntries={["/portfolio/accounts"]}>
        <TabNav ariaLabel="Portfolio sections" tabs={[
          { to: "/portfolio/holdings", label: "Holdings" },
          { to: "/portfolio/accounts", label: "Accounts" },
        ]} />
      </MemoryRouter>,
    );
    expect(screen.getByRole("navigation", { name: "Portfolio sections" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Accounts" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Holdings" })).not.toHaveAttribute("aria-current");
  });

  it("drives ?tab= tab sets and keeps other params", () => {
    render(
      <MemoryRouter initialEntries={["/discover?tab=run&x=1"]}>
        <TabNav ariaLabel="Discover sections" tabs={[
          { param: "shortlist", label: "Shortlist" },
          { param: "run", label: "Run" },
        ]} />
      </MemoryRouter>,
    );
    expect(screen.getByRole("link", { name: "Run" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Shortlist" })).not.toHaveAttribute("aria-current");
    expect(screen.getByRole("link", { name: "Shortlist" }).getAttribute("href")).toBe("/discover?tab=shortlist&x=1");
  });

  it("defaults a ?tab= set to its first tab", () => {
    render(
      <MemoryRouter initialEntries={["/news"]}>
        <TabNav ariaLabel="News sections" tabs={[
          { param: "feed", label: "Feed" },
          { param: "sources", label: "Sources" },
        ]} />
      </MemoryRouter>,
    );
    expect(screen.getByRole("link", { name: "Feed" })).toHaveAttribute("aria-current", "page");
  });
});
