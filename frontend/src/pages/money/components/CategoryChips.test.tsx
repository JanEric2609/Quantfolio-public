import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CategoryChips } from "./CategoryChips";

const categories = [
  { id: "cat-1", name: "Groceries", color: "#F59E0B", icon: "shopping-cart" },
  { id: "cat-2", name: "Travel", color: "#2DD4BF", icon: "plane" },
];

describe("CategoryChips", () => {
  it("renders one chip per category", () => {
    render(<CategoryChips categories={categories} onSelect={vi.fn()} />);
    expect(screen.getByRole("button", { name: /Groceries/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Travel/ })).toBeInTheDocument();
  });

  it("calls onSelect with the category id when tapped", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(<CategoryChips categories={categories} onSelect={onSelect} />);

    await user.click(screen.getByRole("button", { name: /Travel/ }));
    expect(onSelect).toHaveBeenCalledWith("cat-2");
  });

  it("disables all chips when disabled is true", () => {
    render(<CategoryChips categories={categories} disabled onSelect={vi.fn()} />);
    expect(screen.getByRole("button", { name: /Groceries/ })).toBeDisabled();
  });
});
