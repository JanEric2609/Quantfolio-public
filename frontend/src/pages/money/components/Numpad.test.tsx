import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Numpad, formatRawAmount, rawToAmount } from "./Numpad";

describe("formatRawAmount", () => {
  it("formats an empty buffer as zero", () => {
    expect(formatRawAmount("")).toBe("0,00\u00a0€");
  });

  it("treats the raw buffer as cents-first", () => {
    expect(formatRawAmount("350")).toBe("3,50\u00a0€");
    expect(formatRawAmount("5")).toBe("0,05\u00a0€");
    expect(formatRawAmount("40000")).toBe("400,00\u00a0€");
  });
});

describe("rawToAmount", () => {
  it("converts cents-first buffers to euro amounts", () => {
    expect(rawToAmount("")).toBe(0);
    expect(rawToAmount("350")).toBe(3.5);
    expect(rawToAmount("40000")).toBe(400);
  });
});

describe("Numpad", () => {
  it("appends digits and formats via the caller's onChange", async () => {
    const user = userEvent.setup();
    let value = "";
    const onChange = vi.fn((next: string) => { value = next; });
    const { rerender } = render(<Numpad value={value} onChange={onChange} />);

    await user.click(screen.getByRole("button", { name: "Digit 3" }));
    expect(onChange).toHaveBeenLastCalledWith("3");
    rerender(<Numpad value={value} onChange={onChange} />);

    await user.click(screen.getByRole("button", { name: "Digit 5" }));
    expect(onChange).toHaveBeenLastCalledWith("35");
    rerender(<Numpad value={value} onChange={onChange} />);

    await user.click(screen.getByRole("button", { name: "Digit 0" }));
    expect(onChange).toHaveBeenLastCalledWith("350");
  });

  it("removes the last digit on backspace", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Numpad value="350" onChange={onChange} />);

    await user.click(screen.getByRole("button", { name: "Backspace" }));
    expect(onChange).toHaveBeenLastCalledWith("35");
  });

  it("caps the buffer at 9 digits", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Numpad value="123456789" onChange={onChange} />);

    await user.click(screen.getByRole("button", { name: "Digit 1" }));
    expect(onChange).not.toHaveBeenCalled();
  });
});
