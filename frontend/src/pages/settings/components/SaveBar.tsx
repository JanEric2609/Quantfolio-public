import { Loader2 } from "lucide-react";
import { Button } from "../../../components/ui/button";

export function SaveBar({
  changes,
  errors,
  saving,
  onSave,
  onDiscard,
}: {
  changes: number;
  errors: number;
  saving: boolean;
  onSave: () => void;
  onDiscard: () => void;
}) {
  if (!changes) return null;
  return (
    <div
      role="region"
      aria-label="Unsaved changes"
      className="sticky bottom-4 z-20 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border-strong bg-surface-2/95 px-4 py-3 shadow-lg backdrop-blur animate-in fade-in slide-in-from-bottom-2"
    >
      <p className="text-sm text-text-primary">
        <span className="font-medium">
          {changes} unsaved change{changes === 1 ? "" : "s"}
        </span>
        {errors ? (
          <span className="text-danger">
            {" "}
            · fix {errors} field{errors === 1 ? "" : "s"} first
          </span>
        ) : null}
      </p>
      <div className="flex items-center gap-2">
        <Button type="button" variant="ghost" onClick={onDiscard} disabled={saving}>
          Discard
        </Button>
        <Button type="button" onClick={onSave} disabled={saving}>
          {saving ? <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" /> : null}
          Save changes
        </Button>
      </div>
    </div>
  );
}
