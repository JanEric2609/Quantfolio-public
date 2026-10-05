import { useState } from "react";
import { useGoals } from "../hooks/useGoals";
import { useGoalPortfolio } from "../hooks/useGoalPortfolio";
import { useGoalContribution } from "../hooks/useGoalContribution";
import { useGoalRebalance } from "../hooks/useGoalRebalance";
import { formatCurrency, formatPercent } from "../../../lib/format";
import { SummaryStrip } from "../components/SummaryStrip";
import type { MetricItem } from "../components/SummaryStrip";

const fmt = (v?: number | string) => (v == null ? "—" : formatCurrency(Number(v), "EUR"));

const riskColors: Record<string, string> = {
  conservative: "text-sky-400",
  moderate: "text-warn",
  aggressive: "text-danger",
};

const bucketColors: Record<string, string> = {
  short_term: "text-sky-400",
  medium_term: "text-warn",
  long_term: "text-success",
};

export function GoalsView() {
  const {
    data: goals,
    isLoading: isLoadingGoals,
    isError: isGoalsError,
  } = useGoals();
  const [selectedGoal, setSelectedGoal] = useState<string | null>(null);
  const { data: portfolio, isError: isPortfolioError } =
    useGoalPortfolio(selectedGoal);
  const { data: contribution } = useGoalContribution();
  const { data: rebalance } = useGoalRebalance();

  if (isLoadingGoals)
    return (
      <div className="p-4 text-sm text-text-secondary">Loading goals...</div>
    );
  if (isGoalsError)
    return (
      <div className="p-4 text-sm text-danger">
        Failed to load goals. Please try again.
      </div>
    );

  const goalCount = goals?.length ?? 0;
  const progressPct =
    selectedGoal && portfolio?.trajectory?.on_track != null
      ? portfolio.trajectory.on_track
        ? "On Track"
        : "Off Track"
      : "-";

  const metrics: MetricItem[] = [
    { label: "Goals", value: String(goalCount) },
    {
      label: "Selected",
      value: selectedGoal
        ? goals?.find((g) => g.id === selectedGoal)?.title ?? "-"
        : "-",
      mono: false,
    },
    { label: "Status", value: progressPct, mono: false },
    {
      label: "Monthly Invest",
      value: contribution?.recommended_monthly != null
        ? fmt(contribution.recommended_monthly)
        : "-",
    },
  ];

  return (
    <div className="space-y-0">
      <SummaryStrip metrics={metrics} />

      <div className="space-y-4 pt-4">
        {/* Top row: Goals list + Goal Portfolio Plan */}
        <div className="grid grid-cols-1 gap-3 xl:grid-cols-[1fr_1.5fr]">
          <section
            className="rounded-md p-3"
            style={{ background: "rgb(var(--c-surface))" }}
          >
            <h2 className="mb-2 text-sm font-semibold text-text-primary">
              Investment Goals
            </h2>
            {!goals || goals.length === 0 ? (
              <div className="text-xs text-text-muted">
                No goals defined yet. Create goals in the Plan page.
              </div>
            ) : (
              <div className="space-y-2">
                {goals.map((g) => (
                  <button
                    key={g.id}
                    onClick={() => setSelectedGoal(g.id)}
                    className={`w-full rounded p-3 text-left text-xs transition ${
                      selectedGoal === g.id
                        ? "bg-sky-500/10 ring-1 ring-sky-500/30"
                        : "hover:bg-surface-2"
                    }`}
                    style={{
                      background:
                        selectedGoal === g.id
                          ? undefined
                          : "rgb(var(--c-surface-2))",
                    }}
                  >
                    <div className="flex items-center justify-between">
                      <span className="font-medium">{g.title}</span>
                      {g.risk_tolerance && (
                        <span
                          className={`text-[10px] uppercase ${
                            riskColors[g.risk_tolerance] ?? "text-text-secondary"
                          }`}
                        >
                          {g.risk_tolerance}
                        </span>
                      )}
                    </div>
                    <div className="mt-1 text-text-secondary">
                      {g.target_amount && (
                        <span>Target: {fmt(g.target_amount)}</span>
                      )}
                      {g.target_date && (
                        <span className="ml-2">by {g.target_date}</span>
                      )}
                    </div>
                    {g.progress != null && (
                      <div
                        className="mt-1 h-1.5 w-full rounded-full"
                        style={{ background: "rgb(var(--c-surface-3))" }}
                      >
                        <div
                          className="h-1.5 rounded-full bg-sky-500"
                          style={{
                            width: `${Math.min(
                              100,
                              Math.max(
                                0,
                                ((Number(g.progress) || 0) /
                                  (Number(g.target_amount) || 1)) *
                                  100
                              )
                            )}%`,
                          }}
                        />
                      </div>
                    )}
                  </button>
                ))}
              </div>
            )}
          </section>

          <section
            className="rounded-md p-3"
            style={{ background: "rgb(var(--c-surface))" }}
          >
            <h2 className="mb-2 text-sm font-semibold text-text-primary">
              Goal Portfolio Plan
            </h2>
            {isPortfolioError ? (
              <div className="text-xs text-danger">
                Failed to load portfolio plan.
              </div>
            ) : portfolio?.error ? (
              <div className="text-xs text-danger">{portfolio.error}</div>
            ) : !portfolio ? (
              <div className="text-xs text-text-muted">
                Select a goal to see its recommended portfolio allocation and
                savings trajectory.
              </div>
            ) : (
              <div className="space-y-4">
                <div className="grid grid-cols-4 gap-2">
                  {Object.entries(portfolio.allocation ?? {}).map(
                    ([cls, pctVal]) => (
                      <div
                        key={cls}
                        className="rounded p-2 text-center"
                        style={{ background: "rgb(var(--c-surface-2))" }}
                      >
                        <div className="text-[10px] uppercase text-text-muted">
                          {cls}
                        </div>
                        <div
                          className="mt-1 font-bold"
                          style={{
                            fontFamily: '"JetBrains Mono", monospace',
                          }}
                        >
                          {formatPercent(Number(pctVal), { digits: 0 })}
                        </div>
                      </div>
                    )
                  )}
                </div>
                {portfolio.trajectory && (
                  <div className="grid grid-cols-2 gap-2 xl:grid-cols-4">
                    {[
                      [
                        "Months Left",
                        String(portfolio.trajectory.months_remaining),
                      ],
                      [
                        "Required/Month",
                        fmt(portfolio.trajectory.required_monthly),
                      ],
                      [
                        "Projected",
                        fmt(portfolio.trajectory.projected_amount),
                      ],
                      [
                        "On Track",
                        portfolio.trajectory.on_track ? "Yes" : "No",
                      ],
                    ].map(([l, v]) => (
                      <div
                        key={l}
                        className="rounded p-2 text-center"
                        style={{ background: "rgb(var(--c-surface-2))" }}
                      >
                        <div className="text-[10px] text-text-muted">{l}</div>
                        <div
                          className={`font-bold ${
                            l === "On Track"
                              ? portfolio.trajectory.on_track
                                ? "text-success"
                                : "text-danger"
                              : ""
                          }`}
                          style={{
                            fontFamily: '"JetBrains Mono", monospace',
                          }}
                        >
                          {v}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </section>
        </div>

        {/* Contribution split section */}
        {contribution && contribution.per_goal.length > 0 && (
          <section
            className="rounded-md p-3"
            style={{ background: "rgb(var(--c-surface))" }}
          >
            <h2 className="mb-2 text-sm font-semibold text-text-primary">
              Monthly Investment Split
            </h2>
            <div className="mb-2 text-xs text-text-secondary">
              Recommended:{" "}
              <span className="font-bold text-sky-400">
                {fmt(contribution.recommended_monthly)}
              </span>{" "}
              / month across {contribution.total_goals} goals
              {contribution.diagnostics?.method && (
                <span className="ml-2 text-text-muted">
                  (via {contribution.diagnostics.method})
                </span>
              )}
            </div>
            <div className="space-y-2">
              {contribution.per_goal.map((g) => (
                <div
                  key={g.goal_id}
                  className="flex items-center gap-3 rounded p-2 text-xs"
                  style={{ background: "rgb(var(--c-surface-2))" }}
                >
                  <div className="min-w-[140px] font-medium">{g.title}</div>
                  <div className="flex-1">
                    <div className="flex items-center gap-2">
                      <div className="h-2 flex-1 rounded-full bg-surface-2">
                        <div
                          className="h-2 rounded-full bg-sky-500"
                          style={{
                            width: `${Math.min(
                              100,
                              (g.recommended_monthly /
                                (contribution.recommended_monthly || 1)) *
                                100
                            )}%`,
                          }}
                        />
                      </div>
                      <span
                        className="min-w-[60px] text-right"
                        style={{
                          fontFamily: '"JetBrains Mono", monospace',
                        }}
                      >
                        {fmt(g.recommended_monthly)}
                      </span>
                    </div>
                  </div>
                  <div className="min-w-[60px] text-right text-text-muted">
                    {g.existing_monthly > 0
                      ? `(+${fmt(g.gap)})`
                      : "new"}
                  </div>
                </div>
              ))}
            </div>
          </section>
        )}

        {/* Goal buckets + rebalancing section */}
        {rebalance && rebalance.n_goals > 0 && (
          <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
            {/* Goal time buckets */}
            <section
              className="rounded-md p-3"
              style={{ background: "rgb(var(--c-surface))" }}
            >
              <h2 className="mb-2 text-sm font-semibold text-text-primary">
                Goal Time Buckets
              </h2>
              <div className="space-y-3">
                {(
                  [
                    ["short_term", "Short-term (<2yr)", rebalance.goal_buckets.short_term],
                    ["medium_term", "Medium-term (2-7yr)", rebalance.goal_buckets.medium_term],
                    ["long_term", "Long-term (>7yr)", rebalance.goal_buckets.long_term],
                  ] as const
                ).map(([key, label, items]) => (
                  <div key={key}>
                    <div className="flex items-center justify-between text-xs">
                      <span className={bucketColors[key]}>
                        {label}
                      </span>
                      <span className="text-text-muted">
                        {rebalance.bucket_weights?.[key.replace("_term", "")] ??
                          0}
                        %
                      </span>
                    </div>
                    {items.length > 0 ? (
                      <div className="mt-1 space-y-1">
                        {items.map((item) => (
                          <div
                            key={item.title}
                            className="flex items-center justify-between rounded px-2 py-1 text-[11px]"
                            style={{ background: "rgb(var(--c-surface-2))" }}
                          >
                            <span>{item.title}</span>
                            <span className="text-text-secondary">
                              {item.years_left}yr
                            </span>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <div className="mt-1 text-[11px] text-text-muted">
                        No goals in this bucket
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </section>

            {/* Rebalancing suggestions */}
            <section
              className="rounded-md p-3"
              style={{ background: "rgb(var(--c-surface))" }}
            >
              <h2 className="mb-2 text-sm font-semibold text-text-primary">
                Rebalancing Suggestions
              </h2>
              {!rebalance.available ? (
                <div className="text-xs text-text-muted">
                  Connect DKB holdings to see rebalancing suggestions.
                </div>
              ) : rebalance.suggestions.length === 0 ? (
                <div className="text-xs text-success">
                  Portfolio is well-balanced for your goals. No rebalancing needed.
                </div>
              ) : (
                <div className="space-y-2">
                  {rebalance.suggestions.map((s) => (
                    <div
                      key={s.asset_class}
                      className="flex items-center gap-3 rounded p-2 text-xs"
                      style={{ background: "rgb(var(--c-surface-2))" }}
                    >
                      <div className="min-w-[80px] font-medium uppercase">
                        {s.asset_class}
                      </div>
                      <div className="flex-1">
                        <div className="flex items-center gap-1">
                          <span className="text-text-secondary">{s.current}%</span>
                          <span className="text-text-muted">→</span>
                          <span
                            className={
                              s.action === "increase"
                                ? "text-success"
                                : "text-danger"
                            }
                          >
                            {s.target}%
                          </span>
                        </div>
                      </div>
                      <div
                        className={`text-[10px] uppercase ${
                          s.action === "increase"
                            ? "text-success"
                            : "text-danger"
                        }`}
                      >
                        {s.action} {s.drift_pct}%
                      </div>
                    </div>
                  ))}
                </div>
              )}

              {/* Target allocation bar */}
              {rebalance.available && (
                <div className="mt-3">
                  <div className="mb-1 text-[10px] text-text-muted">
                    Target Allocation
                  </div>
                  <div className="flex h-3 w-full overflow-hidden rounded">
                    {Object.entries(rebalance.target_allocation).map(
                      ([cls, pct]) => {
                        const colors: Record<string, string> = {
                          equity: "bg-sky-500",
                          bonds: "bg-warn",
                          cash: "bg-success",
                          alternatives: "bg-purple-500",
                        };
                        return (pct as number) > 0 ? (
                          <div
                            key={cls}
                            className={`${colors[cls] ?? "bg-slate-500"} h-full`}
                            style={{ width: `${pct}%` }}
                            title={`${cls}: ${pct}%`}
                          />
                        ) : null;
                      }
                    )}
                  </div>
                  <div className="mt-1 flex gap-3 text-[10px] text-text-muted">
                    {Object.entries(rebalance.target_allocation).map(
                      ([cls, pct]) =>
                        (pct as number) > 0 ? (
                          <span key={cls}>
                            {cls}: {pct}%
                          </span>
                        ) : null
                    )}
                  </div>
                </div>
              )}
            </section>
          </div>
        )}
      </div>
    </div>
  );
}
