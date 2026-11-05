/**
 * Like `Promise.all(items.map(fn))`, with at most `limit` calls running at once.
 *
 * A page with many positions must not send one request per position at once:
 * each holds a database connection, and a burst of them once used up the API's
 * whole connection pool.
 */
export async function mapLimit<T, R>(items: readonly T[], limit: number, fn: (item: T) => Promise<R>): Promise<R[]> {
  const results = new Array<R>(items.length);
  let next = 0;
  const worker = async () => {
    while (next < items.length) {
      const index = next++;
      results[index] = await fn(items[index]);
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return results;
}

/** How many per-symbol market requests one hook may run at once. */
export const MARKET_REQUEST_LIMIT = 6;
