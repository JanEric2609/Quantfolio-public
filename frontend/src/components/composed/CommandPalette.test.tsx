import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { CommandPalette } from "./CommandPalette";
import { useUiStore } from "../../lib/store";
import { ROUTE_ENTRIES, EXTRA_PAGES } from "../../lib/routeManifest";

const navigate = vi.fn();
const radixDialogWarning = /DialogContent requires a DialogTitle|Missing `Description` or `aria-describedby=\{undefined\}`/;

function expectNoRadixDialogWarnings(spy: ReturnType<typeof vi.spyOn>) {
  expect(spy.mock.calls.flat().map(String).join(" ")).not.toMatch(radixDialogWarning);
}

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return { ...actual, useNavigate: () => navigate };
});

describe("CommandPalette", () => {
  beforeEach(() => { navigate.mockReset(); useUiStore.setState({ searchOpen: true }); });
  it("filters by query and navigates on Enter", async () => {
    const errorSpy = vi.spyOn(console, "error");
    const warnSpy = vi.spyOn(console, "warn");

    render(<MemoryRouter><CommandPalette /></MemoryRouter>);
    const input = screen.getByPlaceholderText(/search/i);
    await userEvent.type(input, "Portfolio");
    await userEvent.keyboard("{Enter}");
    expect(navigate).toHaveBeenCalledWith("/portfolio/holdings");
    expectNoRadixDialogWarnings(errorSpy);
    expectNoRadixDialogWarnings(warnSpy);

    errorSpy.mockRestore();
    warnSpy.mockRestore();
  });

  it("offers every manifest destination, extra pages included", () => {
    render(<MemoryRouter><CommandPalette /></MemoryRouter>);

    for (const entry of ROUTE_ENTRIES) {
      expect(screen.getByRole("option", { name: entry.label })).toBeInTheDocument();
    }
    for (const page of EXTRA_PAGES) {
      expect(screen.getByRole("option", { name: page.label })).toBeInTheDocument();
    }
  });

  it("finds a destination by one of its keywords", async () => {
    render(<MemoryRouter><CommandPalette /></MemoryRouter>);

    await userEvent.type(screen.getByPlaceholderText(/search/i), "kest");
    await userEvent.keyboard("{Enter}");

    expect(navigate).toHaveBeenCalledWith("/tax");
  });
});
