import { Fragment, useMemo, useState, type ReactNode } from "react";
import katex from "katex";
import "katex/dist/katex.min.css";
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
        The code that does it
        {q.data ? <span className="mt-0.5 block break-all font-mono text-xs text-text-muted">{q.data.file}:{q.data.line}</span> : null}
      </summary>
      {q.isError ? <p className="mt-2 text-text-muted">The source could not be loaded.</p> : null}
      {q.data ? <pre className="mt-2 overflow-x-auto rounded bg-surface-2 p-3 font-mono text-xs leading-relaxed text-text-primary">{q.data.source}</pre> : null}
    </details>
  );
}

/** A formula typeset with KaTeX (D6); the plain text stays as its accessible name and as the fallback. */
function Formula({ tex, text }: { tex?: string; text: string }) {
  const html = useMemo(() => {
    if (!tex) return null;
    try {
      return katex.renderToString(tex, { displayMode: true, throwOnError: false, output: "htmlAndMathml" });
    } catch {
      return null;
    }
  }, [tex]);
  if (!html) return <p className="font-mono text-sm text-text-primary">{text}</p>;
  // KaTeX output of a fixed string written in this file, never user input.
  return <div className="overflow-x-auto text-text-primary" title={text} data-testid="lesson-formula" dangerouslySetInnerHTML={{ __html: html }} />;
}

/**
 * One reading per lesson. Each link was opened on 2026-11-12 and its title checked (book chapters by HTTP 200 and page
 * title, papers by their DOI record or a free copy); keep that rule when adding one.
 */
export const READINGS: Record<string, { label: string; url: string }> = {
  returns: { label: "Hanck et al., Introduction to Econometrics with R, ch. 2 (probability, expected values)", url: "https://www.econometrics-with-r.org/2-pt.html" },
  vol: { label: "Hanck et al., ch. 3 (estimators, variance of the sample mean)", url: "https://www.econometrics-with-r.org/3-arosur.html" },
  beta: { label: "Hanck et al., ch. 4 (linear regression with one regressor)", url: "https://www.econometrics-with-r.org/4-lrwor.html" },
  tstat: { label: "Hanck et al., ch. 5 (hypothesis tests and confidence intervals)", url: "https://www.econometrics-with-r.org/5-htaciitslrm.html" },
  nw: { label: "Hanck et al., §15.4 (HAC standard errors)", url: "https://www.econometrics-with-r.org/15.4-hac-standard-errors.html" },
  multi: { label: "Harvey, Liu and Zhu, \u2026and the Cross-Section of Expected Returns (NBER w20592; RFS 2016)", url: "https://www.nber.org/papers/w20592" },
  wf: { label: "Hyndman and Athanasopoulos, Forecasting: Principles and Practice, §5.10 (time series cross-validation)", url: "https://otexts.com/fpp3/tscv.html" },
  eproc: { label: "Ramdas, Grünwald, Vovk and Shafer, Game-theoretic statistics and safe anytime-valid inference (2023)", url: "https://arxiv.org/abs/2210.01948" },
  shrink: { label: "Ledoit and Wolf, Honey, I Shrunk the Sample Covariance Matrix (J. Portfolio Management, 2004)", url: "https://doi.org/10.3905/jpm.2004.110" },
  mc: { label: "Hyndman and Athanasopoulos, §5.5 (prediction intervals from bootstrapped residuals)", url: "https://otexts.com/fpp3/prediction-intervals.html" },
  tax: { label: "§ 20 InvStG, Teilfreistellung (gesetze-im-internet.de)", url: "https://www.gesetze-im-internet.de/invstg_2018/__20.html" },
};

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
export function Lesson({ id, title, misconception, idea, formula, tex, symbols, sourceName, quiz, children }: {
  id: string; title: string; misconception: string; idea: string; formula: string; tex?: string;
  symbols: Array<[string, string]>; sourceName: string; quiz: Quiz; children: ReactNode;
}) {
  return (
    <Card className="space-y-3 p-5" data-testid={`lesson-${id}`}>
      <h2 className="text-base font-semibold text-text-primary">{title}</h2>
      <p className="text-xs text-warn">A common mistake: {misconception}</p>
      <p className="text-sm text-text-secondary">{idea}</p>
      <div className="rounded-md border border-border p-3">
        <Formula tex={tex} text={formula} />
        <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs text-text-secondary">
          {symbols.map(([sym, meaning]) => (<Fragment key={sym}><dt className="font-mono text-text-primary">{sym}</dt><dd>{meaning}</dd></Fragment>))}
        </dl>
      </div>
      <CodeBlock name={sourceName} />
      <div className="space-y-2">{children}</div>
      <QuizBox lessonId={id} quiz={quiz} />
      {READINGS[id] ? (
        <p className="text-xs text-text-muted" data-testid={`lesson-reading-${id}`}>
          Read further:{" "}
          <a href={READINGS[id].url} target="_blank" rel="noopener noreferrer" className="text-accent hover:underline">{READINGS[id].label}</a>
        </p>
      ) : null}
    </Card>
  );
}
