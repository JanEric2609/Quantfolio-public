import type { MandateListItem } from "../../lib/api";

export const MANDATES = ["A", "B"] as const;
export type MandateLetter = (typeof MANDATES)[number];

/** Shared via <Outlet context>; every tab shows both mandates side by side. */
export interface MandatesOutletContext {
  portfolios: MandateListItem[];
  portfoliosLoading: boolean;
}

/** The PaperPortfolio of each mandate (null when it was never created). */
export function portfolioOf(portfolios: MandateListItem[], mandate: MandateLetter): MandateListItem | null {
  return portfolios.find((p) => p.mandate === mandate) ?? null;
}
