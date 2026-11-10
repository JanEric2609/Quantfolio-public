import { useState } from "react";
import { GraduationCap } from "lucide-react";
import { PageHeader } from "../../components/composed/PageHeader";
import { loadProgress, saveProgress, type LearnState } from "./progress";
import { CrashGuard, HitRate, Ladder, LongRun, MonthlyPlanStation, TaxStation, WhereMyMoney, WhyOnList } from "./stations";

const STATIONS = [WhereMyMoney, MonthlyPlanStation, LongRun, WhyOnList, HitRate, Ladder, CrashGuard, TaxStation];

/** Learn how Quantfolio decides by doing it on your own data: answer first, then see the live figure. */
export function LearnPage() {
  const [state, setState] = useState<LearnState>(loadProgress);
  const onAnswer = (id: string, value: number) => {
    const next = { ...state, answers: { ...state.answers, [id]: value }, done: { ...state.done, [id]: true } };
    setState(next);
    saveProgress(next);
  };
  const answered = Object.keys(state.done).length;
  return (
    <div className="space-y-5">
      <PageHeader title="Learn" subtitle="Eight short stations on your own data. Answer first, then see the real figure."
        icon={<GraduationCap className="h-6 w-6" />} />
      <p className="text-xs text-text-muted" data-testid="learn-progress">{answered} answered. Saved in this browser only.</p>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {STATIONS.map((S, i) => <S key={i} state={state} onAnswer={onAnswer} />)}
      </div>
    </div>
  );
}
