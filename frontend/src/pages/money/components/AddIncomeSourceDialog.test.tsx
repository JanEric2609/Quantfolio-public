import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AddIncomeSourceDialog } from "./AddIncomeSourceDialog";

describe("AddIncomeSourceDialog", () => {
  it("submits the form with entered values", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();
    render(<AddIncomeSourceDialog onSave={onSave} open onOpenChange={() => {}} />);

    await user.type(screen.getByLabelText("Name"), "BAföG");
    await user.clear(screen.getByLabelText("Amount"));
    await user.type(screen.getByLabelText("Amount"), "450");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(onSave).toHaveBeenCalledWith(
      expect.objectContaining({ name: "BAföG", amount: 450, cadence: "monthly" })
    );
  });
});
