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

/** Newey-West (Bartlett) t-statistic of the mean. Mirrors quant_metrics.newey_west_t_stat. lags=0 is the plain t. */
export function neweyWestT(x: number[], lags: number): number {
  const n = x.length;
  if (n < 3) return 0;
  const m = mean(x);
  const d = x.map((v) => v - m);
  let lr = d.reduce((a, v) => a + v * v, 0) / n;
  for (let l = 1; l <= Math.min(lags, n - 1); l++) {
    let c = 0;
    for (let i = l; i < n; i++) c += d[i] * d[i - l];
    lr += 2 * (1 - l / (lags + 1)) * (c / n);
  }
  const se = Math.sqrt(Math.max(lr, 0) / n);
  return se < 1e-12 ? 0 : m / se;
}

/** Deterministic generator, so a lesson shows the same example every time. */
export function rng(seed: number): () => number {
  let s = seed >>> 0;
  return () => {
    s = (s + 0x6d2b79f5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function gauss(r: () => number): number {
  return Math.sqrt(-2 * Math.log(1 - r())) * Math.cos(2 * Math.PI * r());
}

/** Best t-statistic among k strategies that all have zero true edge. */
export function bestOfK(k: number, n: number, seed = 7): number {
  const r = rng(seed);
  let best = -Infinity;
  for (let j = 0; j < k; j++) {
    const x = Array.from({ length: n }, () => gauss(r));
    best = Math.max(best, mean(x) / (sampleStd(x) / Math.sqrt(n)));
  }
  return best;
}

/** Walk-forward: estimate the mean on a trailing window, then score the next block with it. */
export function walkForward(x: number[], train: number, test: number): { inSample: number; outSample: number; folds: number } {
  const ins: number[] = [];
  const outs: number[] = [];
  for (let i = 0; i + train + test <= x.length; i += test) {
    const mu = mean(x.slice(i, i + train));
    ins.push(...x.slice(i, i + train).map((v) => Math.sign(mu) * v));
    outs.push(...x.slice(i + train, i + train + test).map((v) => Math.sign(mu) * v));
  }
  return { inSample: ins.length ? mean(ins) : 0, outSample: outs.length ? mean(outs) : 0, folds: Math.floor(outs.length / Math.max(test, 1)) };
}

/** Betting e-process on excess returns scaled by their own lagged volatility. Single bet size lambda. */
export function eProcess(x: number[], lambda: number, burn = 20): number[] {
  const out: number[] = [];
  let e = 1;
  let v = x.slice(0, burn).reduce((a, b) => a + b * b, 0) / Math.max(burn, 1);
  x.forEach((r, t) => {
    if (t >= burn && v > 0) {
      const y = Math.max(-1, Math.min(1, r / Math.sqrt(v) / 4));
      e *= 1 + lambda * y;
    }
    out.push(e);
    v = 0.97 * v + 0.03 * r * r;
  });
  return out;
}

/** Shrink the sample covariance toward its diagonal: (1-s) S + s diag(S). */
export function shrinkToDiagonal(cov: number[][], s: number): number[][] {
  return cov.map((row, i) => row.map((v, j) => (i === j ? v : (1 - s) * v)));
}

export function sampleCov(a: number[], b: number[]): number {
  const ma = mean(a);
  const mb = mean(b);
  return a.reduce((acc, v, i) => acc + (v - ma) * (b[i] - mb), 0) / (a.length - 1);
}

/** Bootstrap fan: resample daily returns with replacement; returns the 5/50/95 % wealth at the horizon. */
export function bootstrapFan(r: number[], horizon: number, paths = 400, seed = 11): { p5: number; p50: number; p95: number } {
  const g = rng(seed);
  const finals: number[] = [];
  for (let p = 0; p < paths; p++) {
    let w = 1;
    for (let t = 0; t < horizon; t++) w *= 1 + r[Math.floor(g() * r.length)];
    finals.push(w);
  }
  finals.sort((a, b) => a - b);
  const q = (f: number) => finals[Math.min(finals.length - 1, Math.floor(f * finals.length))];
  return { p5: q(0.05), p50: q(0.5), p95: q(0.95) };
}
