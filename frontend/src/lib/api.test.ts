import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { Mock } from "vitest";
import { api, apiErrorMessage, toSaveScenarioBody } from "./api";

const fetchMock = fetch as unknown as Mock;

function restoreDefaultFetch() {
  fetchMock.mockReset();
  fetchMock.mockResolvedValue(new Response(JSON.stringify({}), { status: 200 }));
}

describe("api", () => {
  beforeEach(() => {
    fetchMock.mockReset();
  });

  afterEach(() => {
    restoreDefaultFetch();
  });

  it("parses JSON success responses", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200 }));

    await expect(api<{ ok: boolean }>("/ok")).resolves.toEqual({ ok: true });
  });

  it("returns undefined for empty 204 success responses", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));

    await expect(api<void>("/empty")).resolves.toBeUndefined();
  });

  it("keeps parsing error payloads before throwing", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: { message: "Request failed", debug: "trace-id=1" } }), {
        status: 400,
        statusText: "Bad Request",
      })
    );

    await expect(api("/fail")).rejects.toThrow("Request failed\ntrace-id=1");
  });
});

describe("apiErrorMessage", () => {
  it("reads the backend envelope's string message", () => {
    expect(apiErrorMessage({ error: { code: 404, message: "Not found" } }, "x")).toBe("Not found");
  });
  it("unwraps a dict detail instead of dumping JSON", () => {
    // HTTPException(detail={"message": ..., "debug": ...}) arrives as error.message = {...}
    const parsed = { error: { code: 503, message: { message: "LLM unavailable", debug: "timeout after 30s" } } };
    expect(apiErrorMessage(parsed, "x")).toBe("LLM unavailable\ntimeout after 30s");
  });
  it("falls back to the raw body when it is not JSON", () => {
    expect(apiErrorMessage(null, "Bad Gateway")).toBe("Bad Gateway");
  });
});

describe("toSaveScenarioBody", () => {
  it("maps a stochastic run onto the fields the save endpoint reads", () => {
    const body = toSaveScenarioBody({
      type: "stochastic", name: "Bear", S0: 50_000, mu: -0.02, sigma: 0.3, T: 2.4,
      horizon_steps: 100, n_paths: 50_000, strike: 100, nu: 5,
    });
    expect(body).toMatchObject({
      name: "Bear", start_value: 50_000, annual_return: -0.02, annual_volatility: 0.3, years: 2, simulations: 10_000,
    });
  });
});
