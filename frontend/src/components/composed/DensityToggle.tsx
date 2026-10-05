import { ToggleGroup, ToggleGroupItem } from "../ui/toggle-group";
import { useUiStore } from "../../lib/store";

export function DensityToggle() {
  const density = useUiStore((s) => s.density);
  const setDensity = useUiStore((s) => s.setDensity);
  return (
    <ToggleGroup type="single" value={density} onValueChange={(v) => v && setDensity(v as "compact" | "comfortable")} className="gap-0">
      <ToggleGroupItem value="compact" aria-label="Compact density" className="text-xs">Compact</ToggleGroupItem>
      <ToggleGroupItem value="comfortable" aria-label="Comfortable density" className="text-xs">Comfortable</ToggleGroupItem>
    </ToggleGroup>
  );
}
