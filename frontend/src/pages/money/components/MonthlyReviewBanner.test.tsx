import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MonthlyReviewBanner } from "./MonthlyReviewBanner";
import { api } from "../../../lib/api";

vi.mock("../../../lib/api", () => ({ api: vi.fn() }));
vi.mocked(api).mockResolvedValue({});

function renderBanner() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MonthlyReviewBanner />
    </QueryClientProvider>
  );
}

describe("MonthlyReviewBanner", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows the review prompt within the first week of a new month", () => {
    vi.useFakeTimers().setSystemTime(new Date(2026, 7, 3)); // Aug 3, 2026
    renderBanner();

    expect(screen.getByRole("button", { name: "Review" })).toBeInTheDocument();
    expect(screen.getByText("July")).toBeInTheDocument();
  });

  it("hides the prompt after the first week of the month", () => {
    vi.useFakeTimers().setSystemTime(new Date(2026, 7, 15)); // Aug 15, 2026
    renderBanner();

    expect(screen.queryByRole("button", { name: "Review" })).not.toBeInTheDocument();
  });

  it("stays dismissed for the same month after clicking dismiss", () => {
    vi.useFakeTimers().setSystemTime(new Date(2026, 7, 3));
    const { unmount } = renderBanner();

    fireEvent.click(screen.getByRole("button", { name: "Dismiss review prompt" }));
    expect(screen.queryByRole("button", { name: "Review" })).not.toBeInTheDocument();

    unmount();
    renderBanner();
    expect(screen.queryByRole("button", { name: "Review" })).not.toBeInTheDocument();
  });
});
