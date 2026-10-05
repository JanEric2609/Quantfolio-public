import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Button } from "./button";

describe("Button", () => {
  it("renders children and fires onClick", async () => {
    const handler = vi.fn();
    render(<Button onClick={handler}>Click</Button>);
    await userEvent.click(screen.getByRole("button", { name: "Click" }));
    expect(handler).toHaveBeenCalledTimes(1);
  });
  it("renders as anchor when asChild is true", () => {
    render(<Button asChild><a href="/x">Go</a></Button>);
    const link = screen.getByRole("link", { name: "Go" });
    expect(link).toHaveAttribute("href", "/x");
  });
});
