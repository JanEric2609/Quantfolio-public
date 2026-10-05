import { AddHoldingDialog } from "./components/AddHoldingDialog";
import { HoldingsTable } from "./components/HoldingsTable";

export function HoldingsTab() {
  return <HoldingsTable rightSlot={<AddHoldingDialog />} />;
}
