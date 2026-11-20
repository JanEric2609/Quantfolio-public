import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { DeskPage } from "./DeskPage";

describe("DeskPage", () => {
  it("always shows the paper banner and exactly one h1, even when the API answers with nothing", async () => {
    render(<MemoryRouter><DeskPage /></MemoryRouter>);
    expect(await screen.findByText("Paper trading: no real orders are placed")).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("button", { name: /Ask the desk/ })).toBeInTheDocument();
  });
});
