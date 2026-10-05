import { EvolutionTab } from "../advisor/EvolutionTab";
import { TypeNumbers } from "./TypeNumbers";

/** The advisor loop's daily calls: the verdict numbers, then the existing scorecard/evolution view. */
export function AdvisorTab() {
  return (
    <div className="space-y-4">
      <TypeNumbers type="advisor" />
      <EvolutionTab />
    </div>
  );
}
