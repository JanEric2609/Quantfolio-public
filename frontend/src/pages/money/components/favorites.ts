export interface FavoriteExpense {
  key: string;
  description: string;
  amount: number;
  category_id: string | null;
  count: number;
}

interface ExpenseLike {
  date: string;
  description: string;
  amount: string | number;
  category_id?: string | null;
}

export function deriveFavorites(
  expenses: ExpenseLike[],
  options?: { windowDays?: number; limit?: number; now?: Date }
): FavoriteExpense[] {
  const windowDays = options?.windowDays ?? 60;
  const limit = options?.limit ?? 6;
  const now = options?.now ?? new Date();
  const cutoff = new Date(now);
  cutoff.setDate(cutoff.getDate() - windowDays);

  const groups = new Map<string, FavoriteExpense>();
  for (const expense of expenses) {
    const amount = Number(expense.amount);
    if (new Date(expense.date) < cutoff) continue;

    const categoryId = expense.category_id ?? null;
    const normalizedDescription = expense.description.trim().toLowerCase();
    const key = `${categoryId ?? "none"}|${normalizedDescription}|${Math.abs(amount).toFixed(2)}`;

    const existing = groups.get(key);
    if (existing) {
      existing.count += 1;
    } else {
      groups.set(key, {
        key,
        description: expense.description,
        amount: Math.abs(amount),
        category_id: categoryId,
        count: 1,
      });
    }
  }

  return Array.from(groups.values())
    .filter((item) => item.count >= 2)
    .sort((a, b) => b.count - a.count)
    .slice(0, limit);
}
