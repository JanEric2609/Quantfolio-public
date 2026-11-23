/** The Intraday page's position calculator. Pure maths, no network and no order: it only shows what a
 *  manual Trading 212 CFD of a given risk would be. Facts come from the API (`T212Facts`). */

export interface CalcFacts {
  marginRate: number;
  overnightRate: number;
  fxFee: number;
  minOrderEur: number;
}

export interface CalcInput {
  direction: "long" | "short";
  /** EUR lost if the stop is hit. */
  riskEur: number;
  stopPoints: number;
  /** The index level the fill is expected at. */
  level: number;
  /** EUR per index point per unit: 1 for a EUR index, the EUR per USD rate for a USD one. */
  eurPerPoint: number;
  spreadBp: number;
  currency: "EUR" | "USD";
}

export interface CalcResult {
  quantity: number;
  exposure: number;
  margin: number;
  spreadCost: number;
  overnightPerNight: number;
  stopLevel: number;
  fxNote: string | null;
  belowMinimum: boolean;
}

/** Round down to 4 decimals (Trading 212's quantity step), tolerant of binary float noise. */
export const floor4 = (x: number) => Math.floor(x * 10_000 + 1e-9) / 10_000;

export function calcPosition(i: CalcInput, f: CalcFacts): CalcResult | null {
  if (![i.riskEur, i.stopPoints, i.level, i.eurPerPoint].every((v) => Number.isFinite(v) && v > 0)) return null;
  const quantity = floor4(i.riskEur / (i.stopPoints * i.eurPerPoint));
  const exposure = quantity * i.level * i.eurPerPoint;
  return {
    quantity,
    exposure,
    margin: exposure * f.marginRate,
    spreadCost: (i.spreadBp / 10_000) * exposure,
    overnightPerNight: exposure * f.overnightRate,
    stopLevel: i.direction === "long" ? i.level - i.stopPoints : i.level + i.stopPoints,
    fxNote: i.currency === "USD" ? `USD market: ${(f.fxFee * 100).toFixed(1)} % FX fee on the result at close.` : null,
    belowMinimum: exposure < f.minOrderEur,
  };
}

/** "in 3 h 12 min" / "in 15 min" / "passed" for a target time. */
export function countdown(iso: string | null, now: number): string {
  if (!iso) return "-";
  const mins = Math.round((new Date(iso).getTime() - now) / 60_000);
  if (mins <= 0) return "passed";
  return mins >= 60 ? `in ${Math.floor(mins / 60)} h ${mins % 60} min` : `in ${mins} min`;
}
