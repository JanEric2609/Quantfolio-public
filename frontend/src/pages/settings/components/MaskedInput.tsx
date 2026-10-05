import { useState } from "react";
import { Eye, EyeOff, X } from "lucide-react";
import { Input } from "../../../components/ui/input";
import { Button } from "../../../components/ui/button";

export interface MaskedInputProps {
  value: string;
  onChange: (v: string) => void;
  /** Render the "Replace" affordance instead of an empty editable input. */
  configured?: boolean;
  /** Optional label rendered above the input. */
  label?: string;
  placeholder?: string;
  /** `password` by default; `text` for non-secret meta fields. */
  type?: "text" | "password" | "url" | "number";
  ariaLabel?: string;
  /** When true the field stays disabled (e.g. while the form is submitting). */
  disabled?: boolean;
  id?: string;
}

export function MaskedInput({
  value,
  onChange,
  configured = false,
  label,
  placeholder = "Enter value",
  type = "password",
  ariaLabel,
  disabled = false,
  id,
}: MaskedInputProps) {
  const [editing, setEditing] = useState(false);
  const [visible, setVisible] = useState(false);
  const inputType = visible ? "text" : type;

  if (configured && value === "" && !editing) {
    return (
      <div className="space-y-1.5">
        {label && <label className="text-xs text-text-secondary">{label}</label>}
        <div className="flex items-center gap-2">
          <span className="font-mono text-sm text-text-muted select-none">•••••••</span>
          <Button
            id={id}
            type="button"
            variant="outline"
            size="sm"
            onClick={() => setEditing(true)}
            disabled={disabled}
            className="h-7 text-xs"
          >
            Replace
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-1.5">
      {label && <label className="text-xs text-text-secondary">{label}</label>}
      <div className="relative">
        <Input
          id={id}
          type={inputType}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          aria-label={ariaLabel || label}
          autoComplete="off"
          disabled={disabled}
          className="pr-20"
        />
        <div className="absolute right-2 top-1/2 flex -translate-y-1/2 items-center gap-1">
          <button
            type="button"
            onClick={() => setVisible((v) => !v)}
            className="p-1 text-text-muted hover:text-text-primary disabled:opacity-50"
            aria-label={visible ? "Hide value" : "Show value"}
            disabled={disabled}
          >
            {visible ? <EyeOff size={14} /> : <Eye size={14} />}
          </button>
          <button
            type="button"
            onClick={() => {
              setEditing(false);
              onChange("");
            }}
            className="p-1 text-text-muted hover:text-text-primary disabled:opacity-50"
            aria-label="Cancel"
            disabled={disabled}
          >
            <X size={14} />
          </button>
        </div>
      </div>
    </div>
  );
}
