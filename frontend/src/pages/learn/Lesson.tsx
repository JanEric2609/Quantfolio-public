import { Fragment, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { Card } from "../../components/ui/card";
import { isDue, loadQuiz, recordAnswer, REVIEW_AFTER_DAYS, type QuizState } from "./quizSchedule";

export interface SourceOut { name: string; file: string; line: number; source: string }

/** The function that implements the idea, shown read-only with its file and line (allow-listed on the server). */
function CodeBlock({ name }: { name: string }) {
  const q = useQuery({
    queryKey: ["learn", "source", name],
    queryFn: () => api<SourceOut>(`/api/learn/source?name=${encodeURIComponent(name)}`),
    staleTime: Infinity,
  });
  return (
    <details className="rounded-md border border-border p-3 text-sm" data-testid={`lesson-code-${name}`}>
      <summary className="cursor-pointer text-text-primary">
        The code that does it{q.data ? <span className="ml-2 font-mono text-xs text-text-muted">{q.data.file}:{q.data.line}</span> : null}
      </summary>
      {q.isError ? <p className="mt-2 text-text-muted">The source could not be loaded.</p> : null}
      {q.data ? <pre className="mt-2 overflow-x-auto rounded bg-surface-2 p-3 font-mono text-xs leading-relaxed text-text-primary">{q.data.source}</pre> : null}
    </details>
  );
}

export interface Quiz { question: string; options: string[]; correct: number; explanation: string }

/** One question per lesson; it comes back 2 days, 1 week and 3 weeks after the first answer. */
function QuizBox({ lessonId, quiz }: { lessonId: string; quiz: Quiz }) {
  const [state, setState] = useState<QuizState>(loadQuiz);
  const [picked, setPicked] = useState<number | null>(null);
  const record = state[lessonId];
  const due = isDue(record, Date.now());
  if (!due && picked == null) {
    const next = record ? REVIEW_AFTER_DAYS[record.reviews] : null;
    return <p className="text-xs text-text-muted" data-testid={`quiz-${lessonId}-waiting`}>Quiz answered. It comes back {next ? `${next} days after your first answer` : "no more"}.</p>;
  }
  return (
    <div className="space-y-2 rounded-md bg-surface-2 p-3 text-sm" data-testid={`quiz-${lessonId}`}>
      <p className="font-medium text-text-primary">{record ? "Quiz again: " : "Quiz: "}{quiz.question}</p>
      <div className="space-y-1">
        {quiz.options.map((o, i) => (
          <button
            key={o} type="button" disabled={picked != null}
            onClick={() => { setPicked(i); setState((s) => recordAnswer(s, lessonId, i === quiz.correct, Date.now())); }}
            className="block w-full rounded border border-border px-3 py-1.5 text-left text-text-secondary hover:border-accent disabled:opacity-80"
          >{o}</button>
        ))}
      </div>
      {picked != null ? (
        <p role="status" className="text-text-primary">{picked === quiz.correct ? "Right. " : "Not quite. "}{quiz.explanation}</p>
      ) : null}
    </div>
  );
}

/** A lesson: the idea, the formula with every symbol named, the code, the result on your own data, a quiz. */
export function Lesson({ id, title, misconception, idea, formula, symbols, sourceName, quiz, children }: {
  id: string; title: string; misconception: string; idea: string; formula: string;
  symbols: Array<[string, string]>; sourceName: string; quiz: Quiz; children: ReactNode;
}) {
  return (
    <Card className="space-y-3 p-5" data-testid={`lesson-${id}`}>
      <h2 className="text-base font-semibold text-text-primary">{title}</h2>
      <p className="text-xs text-warn">A common mistake: {misconception}</p>
      <p className="text-sm text-text-secondary">{idea}</p>
      <div className="rounded-md border border-border p-3">
        <p className="font-mono text-sm text-text-primary">{formula}</p>
        <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs text-text-secondary">
          {symbols.map(([sym, meaning]) => (<Fragment key={sym}><dt className="font-mono text-text-primary">{sym}</dt><dd>{meaning}</dd></Fragment>))}
        </dl>
      </div>
      <CodeBlock name={sourceName} />
      <div className="space-y-2">{children}</div>
      <QuizBox lessonId={id} quiz={quiz} />
    </Card>
  );
}
