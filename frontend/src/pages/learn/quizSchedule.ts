/** Spaced quiz: a lesson's question comes back 2 days, 1 week and 3 weeks after it was first answered. */
const KEY = "quantfolio:learn:quiz";
const DAY = 86_400_000;
export const REVIEW_AFTER_DAYS = [2, 7, 21] as const;

export interface QuizRecord { firstAt: number; reviews: number; lastCorrect: boolean }
export type QuizState = Record<string, QuizRecord>;

export function loadQuiz(): QuizState {
  try {
    const raw = window.localStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as QuizState) : {};
  } catch {
    return {};
  }
}

function save(state: QuizState): void {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(state));
  } catch {
    /* blocked storage: the quiz still works, it just forgets */
  }
}

/** Is the next review of this lesson's question due at `now`? A lesson never answered is always "due" (first ask). */
export function isDue(record: QuizRecord | undefined, now: number): boolean {
  if (!record) return true;
  if (record.reviews >= REVIEW_AFTER_DAYS.length) return false;
  return now >= record.firstAt + REVIEW_AFTER_DAYS[record.reviews] * DAY;
}

export function recordAnswer(state: QuizState, id: string, correct: boolean, now: number): QuizState {
  const prev = state[id];
  const next: QuizState = {
    ...state,
    [id]: prev
      ? { ...prev, reviews: prev.reviews + 1, lastCorrect: correct }
      : { firstAt: now, reviews: 0, lastCorrect: correct },
  };
  save(next);
  return next;
}
