import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const apiMock = vi.fn();
vi.mock("../../../lib/api", () => ({ api: (...args: unknown[]) => apiMock(...args) }));

import { FeedManager } from "./FeedManager";

const A = "https://a.example/rss";
const B = "https://b.example/rss";

function renderManager() {
  let list = [A, B];
  apiMock.mockReset();
  apiMock.mockImplementation((url: string, init?: RequestInit) => {
    if (url === "/api/news/feeds" && init?.method === "PUT") {
      list = JSON.parse(String(init.body)).urls;
    }
    if (url === "/api/news/feeds") {
      return Promise.resolve({
        source: "settings",
        feeds: list.map((u) => ({ url: u, title: "", status: "ok", items: 3 })),
      });
    }
    return Promise.reject(new Error(url));
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <FeedManager />
    </QueryClientProvider>,
  );
}

const puts = () => apiMock.mock.calls.filter(([, init]) => (init as RequestInit | undefined)?.method === "PUT")
  .map(([, init]) => JSON.parse(String((init as RequestInit).body)).urls);

describe("FeedManager", () => {
  it("adds, edits and removes feeds by saving the whole list", async () => {
    const user = userEvent.setup();
    renderManager();
    expect(await screen.findByText(A)).toBeInTheDocument();

    await user.type(screen.getByLabelText("New feed URL"), "https://c.example/rss");
    await user.click(screen.getByRole("button", { name: /Add/ }));
    expect(puts().at(-1)).toEqual([A, B, "https://c.example/rss"]);

    await user.click(await screen.findByRole("button", { name: `Edit ${B}` }));
    const input = screen.getByLabelText("Feed URL");
    await user.clear(input);
    await user.type(input, "https://b2.example/rss");
    await user.click(screen.getByRole("button", { name: "Save feed" }));
    expect(puts().at(-1)).toEqual([A, "https://b2.example/rss", "https://c.example/rss"]);

    const row = (await screen.findByText(A)).closest("li") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: `Remove ${A}` }));
    expect(puts().at(-1)).toEqual(["https://b2.example/rss", "https://c.example/rss"]);
  });
});
