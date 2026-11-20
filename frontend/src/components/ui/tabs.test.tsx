import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "./tabs";

describe("Tabs", () => {
  it("swaps content when a trigger is clicked", async () => {
    render(
      <Tabs defaultValue="a">
        <TabsList>
          <TabsTrigger value="a">A</TabsTrigger>
          <TabsTrigger value="b">B</TabsTrigger>
        </TabsList>
        <TabsContent value="a">PANEL A</TabsContent>
        <TabsContent value="b">PANEL B</TabsContent>
      </Tabs>
    );
    expect(screen.getByText("PANEL A")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "A" })).toHaveAttribute("data-state", "active");
    await userEvent.click(screen.getByRole("tab", { name: "B" }));
    expect(screen.getByRole("tab", { name: "B" })).toHaveAttribute("data-state", "active");
    expect(screen.getByText("PANEL B")).toBeInTheDocument();
  });
});
