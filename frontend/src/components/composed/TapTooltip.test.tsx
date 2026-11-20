import { describe, expect, it } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TapTooltip } from "./TapTooltip";
import { GlossaryTooltip } from "./GlossaryTooltip";
import { KpiTile } from "./KpiTile";

describe("TapTooltip", () => {
  it("renders a real, focusable button that is named by aria-label", () => {
    render(
      <TapTooltip content="Explains the metric" ariaLabel="About Sharpe">
        <span aria-hidden>?</span>
      </TapTooltip>,
    );
    const trigger = screen.getByRole("button", { name: "About Sharpe" });
    expect(trigger).toHaveAttribute("type", "button");
    expect(trigger.tabIndex).toBe(0);
    expect(screen.queryByRole("tooltip")).toBeNull();
  });

  it("opens on a touch tap and closes on the next tap (no hover needed)", async () => {
    render(
      <TapTooltip content="Explains the metric" ariaLabel="About Sharpe">
        <span aria-hidden>?</span>
      </TapTooltip>,
    );
    const trigger = screen.getByRole("button", { name: "About Sharpe" });

    fireEvent.pointerDown(trigger, { pointerType: "touch" });
    fireEvent.click(trigger);
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Explains the metric");

    fireEvent.pointerDown(trigger, { pointerType: "touch" });
    fireEvent.click(trigger);
    await waitFor(() => expect(screen.queryByRole("tooltip")).toBeNull());
  });

  it("opens on keyboard focus, closes on Escape and re-opens with Enter", async () => {
    const user = userEvent.setup();
    render(
      <TapTooltip content="Explains the metric" ariaLabel="About Sharpe">
        <span aria-hidden>?</span>
      </TapTooltip>,
    );

    await user.tab();
    expect(screen.getByRole("button", { name: "About Sharpe" })).toHaveFocus();
    expect(await screen.findByRole("tooltip")).toBeInTheDocument();

    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("tooltip")).toBeNull());

    await user.keyboard("{Enter}");
    expect(await screen.findByRole("tooltip")).toBeInTheDocument();
  });

  it("keeps the tooltip open after a mouse click (click pins what hover showed)", async () => {
    const user = userEvent.setup();
    render(
      <TapTooltip content="Explains the metric" ariaLabel="About Sharpe">
        <span aria-hidden>?</span>
      </TapTooltip>,
    );
    await user.click(screen.getByRole("button", { name: "About Sharpe" }));
    expect(await screen.findByRole("tooltip")).toBeInTheDocument();
  });
});

describe("GlossaryTooltip", () => {
  it("is a button named by its text and shows the glossary entry on tap", async () => {
    render(<GlossaryTooltip k="sharpe">Sharpe</GlossaryTooltip>);
    const trigger = screen.getByRole("button", { name: "Sharpe" });
    fireEvent.pointerDown(trigger, { pointerType: "touch" });
    fireEvent.click(trigger);
    expect(await screen.findByRole("tooltip")).toHaveTextContent(/Excess return per unit of total volatility/);
  });

  it("renders bare children for an unknown key", () => {
    render(<GlossaryTooltip k="not_a_term">plain</GlossaryTooltip>);
    expect(screen.getByText("plain")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });
});

describe("KpiTile tooltip trigger", () => {
  it("exposes the glossary help as a labelled button instead of a bare span", () => {
    render(<KpiTile label="Sharpe" value={1.2} glossaryKey="sharpe" />);
    expect(screen.getByRole("button", { name: "About Sharpe" })).toBeInTheDocument();
  });

  it("exposes the metric help as a labelled button", () => {
    render(<KpiTile label="Sortino" value={1.2} metricKey="sortino" />);
    expect(screen.getAllByRole("button").length).toBeGreaterThan(0);
    expect(screen.getAllByRole("button")[0]).toHaveAttribute("aria-label");
  });

  it("shows a delta with sign, arrow and a spoken label, not colour alone", () => {
    render(<KpiTile label="YTD" value="12.000 €" delta={-0.031} />);
    expect(screen.getByText("-3,10 %")).toBeInTheDocument();
    expect(screen.getByText("▼")).toBeInTheDocument();
    expect(document.querySelector(".sr-only")).toHaveTextContent("down -3,10 %");
  });
});
