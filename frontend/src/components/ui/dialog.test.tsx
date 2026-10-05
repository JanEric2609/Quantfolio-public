import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Dialog, DialogTrigger, DialogContent, DialogTitle } from "./dialog";

const radixDialogWarning = /DialogContent requires a DialogTitle|Missing `Description` or `aria-describedby=\{undefined\}`/;

function expectNoRadixDialogWarnings(spy: ReturnType<typeof vi.spyOn>) {
  expect(spy.mock.calls.flat().map(String).join(" ")).not.toMatch(radixDialogWarning);
}

describe("Dialog", () => {
  it("opens on trigger and closes on Escape", async () => {
    const errorSpy = vi.spyOn(console, "error");
    const warnSpy = vi.spyOn(console, "warn");

    render(
      <Dialog>
        <DialogTrigger>Open</DialogTrigger>
        <DialogContent aria-describedby={undefined}>
          <DialogTitle>Example dialog</DialogTitle>
          Hello
        </DialogContent>
      </Dialog>
    );
    await userEvent.click(screen.getByRole("button", { name: "Open" }));
    expect(screen.getByText("Hello")).toBeInTheDocument();
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(screen.queryByText("Hello")).not.toBeInTheDocument();
    expectNoRadixDialogWarnings(errorSpy);
    expectNoRadixDialogWarnings(warnSpy);

    errorSpy.mockRestore();
    warnSpy.mockRestore();
  });
});
