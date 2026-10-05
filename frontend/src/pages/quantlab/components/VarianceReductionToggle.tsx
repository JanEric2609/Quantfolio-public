import { ToggleGroup, ToggleGroupItem } from "../../../components/ui/toggle-group";
import { GlossaryTooltip } from "../../../components/composed/GlossaryTooltip";

const VR_TECHNIQUES = [
  { value: "antithetic", label: "Antithetic", key: "antithetic" },
  { value: "control_variate", label: "Control Variate", key: "control_variate" },
  { value: "importance_sampling", label: "Importance", key: "importance_sampling" },
  { value: "stratified", label: "Stratified", key: "stratified" },
];

interface VarianceReductionToggleProps {
  value: string[];
  onChange: (techniques: string[]) => void;
}

export function VarianceReductionToggle({ value, onChange }: VarianceReductionToggleProps) {
  return (
    <div className="space-y-2">
      <label className="text-xs font-semibold text-text-primary">
        <GlossaryTooltip k="variance_reduction">Variance Reduction</GlossaryTooltip>
      </label>
      <ToggleGroup
        type="multiple"
        value={value}
        onValueChange={onChange}
        className="justify-start gap-2"
      >
        {VR_TECHNIQUES.map((tech) => (
          <ToggleGroupItem
            key={tech.value}
            value={tech.value}
            aria-label={`Toggle ${tech.label}`}
            className="rounded-md border border-line bg-surface px-3 py-1.5 text-xs data-[state=on]:bg-warn/40 data-[state=on]:text-warn"
          >
            <GlossaryTooltip k={tech.key}>{tech.label}</GlossaryTooltip>
          </ToggleGroupItem>
        ))}
      </ToggleGroup>
    </div>
  );
}
