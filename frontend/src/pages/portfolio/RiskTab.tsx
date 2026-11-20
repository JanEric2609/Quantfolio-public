import { Link } from "react-router-dom";
import { ArrowUpRight, Briefcase } from "lucide-react";
import { Skeleton } from "../../components/ui/skeleton";
import { EmptyState } from "../../components/composed/EmptyState";
import { ErrorState } from "../../components/composed/ErrorState";
import { RiskAlerts } from "./components/risk/RiskAlerts";
import { AssetTypeCard, ConcentrationCard, CurrencyCard, StressCard } from "./components/risk/RiskExposure";
import { ReturnBasisNote, RiskMetrics } from "./components/risk/RiskMetrics";
import { useRiskAssessment, useStressTest } from "./useRisk";

function RiskSkeleton() {
  return (
    <div className="space-y-4" aria-busy="true" aria-label="Loading risk">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        {[0, 1, 2, 3, 4, 5].map((i) => (
          <Skeleton key={i} className="h-24 w-full rounded-md" />
        ))}
      </div>
      <Skeleton className="h-48 w-full rounded-md" />
    </div>
  );
}

/**
 * Portfolio -> Risk: how exposed is the portfolio as it stands? VaR and
 * drawdown come from the price history of the current holdings (never from
 * account balances); concentration and currency exposure look through index
 * ETFs. Replaces the risk half of the old Verification page.
 */
export function RiskTab() {
  const risk = useRiskAssessment();
  const stress = useStressTest();

  if (risk.isLoading) return <RiskSkeleton />;
  if (risk.isError || !risk.data) {
    return (
      <ErrorState
        title="Couldn't load the risk view"
        body={risk.error instanceof Error ? risk.error.message : undefined}
        onRetry={() => risk.refetch()}
      />
    );
  }

  const data = risk.data;
  const hasHoldings = Object.keys(data.asset_type_exposure).length > 0;
  if (!hasHoldings && data.return_basis.n_obs === 0) {
    return (
      <EmptyState
        icon={Briefcase}
        title="No holdings to assess yet"
        description="Add holdings, or sync your broker, and risk appears here: VaR, drawdown, concentration and currency exposure."
      />
    );
  }

  return (
    <div className="space-y-5">
      <section aria-labelledby="risk-returns" className="space-y-3">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 id="risk-returns" className="text-sm font-semibold text-text-primary">
            Loss and drawdown
          </h2>
          <Link to="/quantlab/risk" className="inline-flex items-center gap-1 text-xs text-accent hover:underline">
            Advanced risk in Quant Lab
            <ArrowUpRight className="h-3 w-3" aria-hidden="true" />
          </Link>
        </div>
        <ReturnBasisNote risk={data} />
        <RiskMetrics risk={data} />
      </section>

      <RiskAlerts alerts={data.alerts} />

      <div className="grid gap-4 lg:grid-cols-2">
        <ConcentrationCard risk={data} />
        <CurrencyCard risk={data} />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <StressCard stress={stress.data} loading={stress.isLoading} />
        <AssetTypeCard risk={data} />
      </div>
    </div>
  );
}
