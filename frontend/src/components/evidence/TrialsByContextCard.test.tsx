import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TrialsByContextCard } from "./TrialsByContextCard";

describe("TrialsByContextCard", () => {
  it("lists every context with its count and share of N, Discover scoring included", () => {
    render(<TrialsByContextCard byContext={{ discover_replay: 12, diagnostic: 1 }} acTrials={6217} resolved={6230} />);
    const card = screen.getByTestId("trials-by-context");
    expect(card).toHaveTextContent("N = 6.230");
    const items = screen.getAllByRole("listitem");
    expect(items[0]).toHaveTextContent("Discover factor scoring");
    expect(items[0]).toHaveTextContent("6.217 · 100 %");
    expect(items[1]).toHaveTextContent("discover_replay");
    expect(items[1]).toHaveTextContent("12 · 0,2 %");
  });
});
