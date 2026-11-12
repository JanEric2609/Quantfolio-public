/** The arithmetic the first lessons show, on the reader's own series. Mirrors backend/app/foundation/quant_metrics.py. */

export function dailyReturns(levels: number[]): number[] {
  const out: number[] = [];
  for (let i = 1; i < levels.length; i++) {
    if (levels[i - 1] > 0 && Number.isFinite(levels[i]) && Number.isFinite(levels[i - 1])) out.push(levels[i] / levels[i - 1] - 1);
  }
  return out;
}

const mean = (x: number[]) => x.reduce((a, b) => a + b, 0) / x.length;

export function sampleStd(x: number[]): number {
  if (x.length < 2) return 0;
  const m = mean(x);
  return Math.sqrt(x.reduce((a, b) => a + (b - m) ** 2, 0) / (x.length - 1));
}

/** Geometric: prod(1+r)^(P/N) - 1. */
export function annualisedReturn(r: number[], periods = 252): number {
  if (r.length === 0 || r.some((v) => 1 + v <= 0)) return 0;
  const logSum = r.reduce((a, v) => a + Math.log(1 + v), 0);
  return Math.exp((logSum * periods) / r.length) - 1;
}

/** Arithmetic shortcut that overstates: mean(r) * P. The gap is the volatility drag. */
export function arithmeticAnnual(r: number[], periods = 252): number {
  return r.length ? mean(r) * periods : 0;
}

export function annualisedVolatility(r: number[], periods = 252): number {
  return sampleStd(r) * Math.sqrt(periods);
}

export interface Ols { beta: number; alpha: number; r2: number; seBeta: number; t: number; n: number }

/** r_p = alpha + beta * r_b + e (daily). alpha is per day. */
export function ols(p: number[], b: number[]): Ols | null {
  const n = Math.min(p.length, b.length);
  if (n < 3) return null;
  const y = p.slice(-n);
  const x = b.slice(-n);
  const mx = mean(x);
  const my = mean(y);
  const sxx = x.reduce((a, v) => a + (v - mx) ** 2, 0);
  if (sxx === 0) return null;
  const sxy = x.reduce((a, v, i) => a + (v - mx) * (y[i] - my), 0);
  const beta = sxy / sxx;
  const alpha = my - beta * mx;
  const rss = y.reduce((a, v, i) => a + (v - alpha - beta * x[i]) ** 2, 0);
  const tss = y.reduce((a, v) => a + (v - my) ** 2, 0);
  const seBeta = Math.sqrt(rss / (n - 2) / sxx);
  return { beta, alpha, r2: tss === 0 ? 0 : 1 - rss / tss, seBeta, t: seBeta === 0 ? 0 : beta / seBeta, n };
}
