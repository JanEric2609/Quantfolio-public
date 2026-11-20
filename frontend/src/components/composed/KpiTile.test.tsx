import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { KpiTile } from "./KpiTile";

describe("KpiTile", () => {
  it("renders label and value", () => {
    render(<KpiTile label="YTD" value="€12,345" />);
    expect(screen.getByText("YTD")).toBeInTheDocument();
    expect(screen.getByText("€12,345")).toBeInTheDocument();
  });
  it("formats delta as percent by default", () => {
    render(<KpiTile label="YTD" value="€12,345" delta={0.052} />);
    expect(screen.getByText("+5,20 %")).toBeInTheDocument();
  });
  it("formats delta as a number when deltaFormat='number'", () => {
    render(<KpiTile label="Δ" value="€12,345" delta={3} deltaFormat="number" />);
    expect(screen.getByText("+3,00")).toBeInTheDocument();
  });
  it("renders glossary trigger when glossaryKey is provided", () => {
    render(<KpiTile label="Sharpe" value={1.2} glossaryKey="sharpe" />);
    // HelpCircle has aria-hidden by default; test by querying for the data attribute we set.
    expect(document.querySelector("[data-glossary-key='sharpe']")).toBeTruthy();
  });
});
