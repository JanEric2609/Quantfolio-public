/** Progress and last answers of the Learn page, kept in this browser only. Every access is guarded: storage can be blocked. */
const KEY = "quantfolio:learn";

export interface LearnState {
  done: Record<string, boolean>;
  answers: Record<string, number>;
}

export function loadProgress(): LearnState {
  try {
    const raw = window.localStorage.getItem(KEY);
    const parsed = raw ? (JSON.parse(raw) as Partial<LearnState>) : {};
    return { done: parsed.done ?? {}, answers: parsed.answers ?? {} };
  } catch {
    return { done: {}, answers: {} };
  }
}

export function saveProgress(state: LearnState): void {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(state));
  } catch {
    /* blocked storage: the page still works, it just forgets */
  }
}
