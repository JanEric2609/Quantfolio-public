import { useQuery } from "@tanstack/react-query";
import { useOutletContext } from "react-router-dom";
import { api } from "../../lib/api";
import { formatCurrency } from "../../lib/format";
import { Metric } from "../../components/Metric";
import { MetricGroup } from "../../components/composed/MetricGroup";
import { Card, CardContent } from "../../components/ui/card";
import { AnnualSankeyChart } from "./components/AnnualSankeyChart";
import { BufferMetricCard } from "./components/BufferMetricCard";
import { EnvelopeHistoryPanel } from "./components/EnvelopeHistoryPanel";
import { MonthlyReviewBanner } from "./components/MonthlyReviewBanner";
import { SurplusSuggestion } from "./components/SurplusSuggestion";
import { UpcomingRenewals } from "./components/UpcomingRenewals";

interface BudgetSummary {
  spent: number;
  active_subscription_run_rate: number;
  open_invoice_total: number;
  open_invoice_count: number;
  investable_surplus_estimate: number;
  income: number;
  net_income: number;
  expense_count: number;
}

interface Subscription {
  id: string;
  name: string;
  amount: string;
  currency: string;
  billing_cycle: string;
  next_due_date: string;
  category_id?: string;
  active: boolean;
}

export function OverviewTab() {
  const { searchParams } = useOutletContext<{ searchParams: URLSearchParams }>();

  const summary = useQuery({
    queryKey: ["budget-summary"],
    queryFn: () => api<BudgetSummary>("/api/budget/summary"),
  });

  const subscriptions = useQuery({
    queryKey: ["subscriptions"],
    queryFn: () => api<Subscription[]>("/api/budget/subscriptions"),
  });

  const upcomingSubscriptions = (subscriptions.data ?? [])
    .filter((s) => {
      if (!s.active) return false;
      const due = new Date(s.next_due_date).getTime();
      const in14d = Date.now() + 14 * 24 * 60 * 60 * 1000;
      return due <= in14d;
    });

  const netIncome = summary.data?.net_income ?? 0;

  return (
    <div className="space-y-6">
      <MonthlyReviewBanner />

      <MetricGroup>
        <Metric
          label="Monthly Spend"
          value={formatCurrency(summary.data?.spent ?? undefined)}
          tone="warn"
        />
        <Metric
          label="Subscriptions Run Rate"
          value={formatCurrency(summary.data?.active_subscription_run_rate ?? undefined)}
        />
        <Metric
          label="Unpaid Invoices"
          value={formatCurrency(summary.data?.open_invoice_total ?? undefined)}
          tone={summary.data?.open_invoice_count ? "bad" : "neutral"}
        />
        <Metric
          label="Net Income"
          value={formatCurrency(netIncome)}
          tone={netIncome >= 0 ? "good" : "bad"}
        />
        <BufferMetricCard />
      </MetricGroup>

      {summary.data && <SurplusSuggestion amount={summary.data.investable_surplus_estimate} />}

      <Card>
        <CardContent className="pt-6">
          <AnnualSankeyChart />
        </CardContent>
      </Card>

      <EnvelopeHistoryPanel />

      {upcomingSubscriptions.length > 0 && (
        <UpcomingRenewals
          items={upcomingSubscriptions.map((s) => ({
            id: s.id,
            name: s.name,
            amount: s.amount,
            currency: s.currency,
            next_due_date: s.next_due_date,
          }))}
        />
      )}
    </div>
  );
}
