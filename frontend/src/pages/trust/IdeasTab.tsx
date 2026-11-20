import { SkillTrendPanel } from "../../components/discover/SkillTrendPanel";
import { TypeNumbers } from "./TypeNumbers";

/** Discover picks: the verdict numbers, then the existing prediction-skill view. */
export function IdeasTab() {
  return (
    <div className="space-y-4">
      <TypeNumbers type="ideas" />
      <SkillTrendPanel />
    </div>
  );
}
