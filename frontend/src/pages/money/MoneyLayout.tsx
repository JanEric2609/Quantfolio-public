import { Outlet, useSearchParams, useLocation } from "react-router-dom";
import { PageHeader } from "../../components/composed/PageHeader";
import { TabNav } from "../../components/composed/TabNav";
import { QuickAddExpense } from "./components/QuickAddExpense";

const TABS = [
  { to: "/money/overview", label: "Overview" },
  { to: "/money/envelopes", label: "Envelopes" },
  { to: "/money/income", label: "Income" },
  { to: "/money/expenses", label: "Expenses" },
  { to: "/money/subscriptions", label: "Subscriptions" },
  { to: "/money/invoices", label: "Invoices" },
];

export function MoneyLayout() {
  const [searchParams] = useSearchParams();
  const location = useLocation();
  return (
    <div className="space-y-6">
      <PageHeader title="Budget" actions={<QuickAddExpense />} />
      <TabNav tabs={TABS.map((tab) => ({ ...tab, to: `${tab.to}${location.search}` }))} ariaLabel="Budget sections" />
      <Outlet context={{ searchParams }} />
    </div>
  );
}
