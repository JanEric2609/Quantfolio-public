import { afterEach, describe, expect, it } from "vitest";
import { act, render, screen } from "@testing-library/react";
import { OfflineBanner } from "./OfflineBanner";

function setOnline(value: boolean) {
  Object.defineProperty(window.navigator, "onLine", { value, configurable: true });
  window.dispatchEvent(new Event(value ? "online" : "offline"));
}

describe("OfflineBanner", () => {
  afterEach(() => setOnline(true));

  it("shows nothing while online", () => {
    render(<OfflineBanner />);

    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("appears when the browser goes offline and disappears when it is back", () => {
    render(<OfflineBanner />);

    act(() => setOnline(false));
    expect(screen.getByRole("status")).toHaveTextContent(/offline/i);
    expect(screen.getByRole("status")).toHaveTextContent(/may be out of date/i);

    act(() => setOnline(true));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
