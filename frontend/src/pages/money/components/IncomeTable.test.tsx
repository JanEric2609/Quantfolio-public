import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { IncomeTable, type IncomeSourceRow } from "./IncomeTable";

const rows: IncomeSourceRow[] = [
  {
    id: "inc-1",
    name: "BAföG",
    amount: "450",
    currency: "EUR",
    cadence: "monthly",
    next_date: "2026-09-01",
    active: true,
  },
];

describe("IncomeTable", () => {
  it("renders income source rows", () => {
    render(<IncomeTable data={rows} />);
    expect(screen.getByText("BAföG")).toBeInTheDocument();
    expect(screen.getByText("monthly")).toBeInTheDocument();
  });

  it("shows an empty state with no rows", () => {
    render(<IncomeTable data={[]} />);
    expect(screen.getByText("No income sources.")).toBeInTheDocument();
  });

  it("calls onDelete when the delete action is confirmed", async () => {
    const user = userEvent.setup();
    const onDelete = vi.fn();
    render(<IncomeTable data={rows} onDelete={onDelete} />);

    // The trigger button and the AlertDialogAction confirm button are both
    // labeled "Delete" — click the trigger first, then the confirm button
    // that appears once the dialog opens (the last "Delete" button in the DOM).
    await user.click(screen.getAllByRole("button", { name: "Delete" })[0]);
    const confirmButtons = screen.getAllByRole("button", { name: "Delete" });
    await user.click(confirmButtons[confirmButtons.length - 1]);

    expect(onDelete).toHaveBeenCalledWith("inc-1");
  });
});
