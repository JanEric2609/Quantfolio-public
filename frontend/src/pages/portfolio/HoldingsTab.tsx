import { AddHoldingDialog } from "./components/AddHoldingDialog";
import { DerivativesCard } from "./components/DerivativesCard";
import { HoldingsTable } from "./components/HoldingsTable";

export function HoldingsTab() {
  return (
    <div className="space-y-4">
      <HoldingsTable rightSlot={<AddHoldingDialog />} />
      <DerivativesCard />
    </div>
  );
}
