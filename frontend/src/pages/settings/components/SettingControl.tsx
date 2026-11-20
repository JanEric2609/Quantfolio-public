import type { SettingsCatalogEntry } from "../../../lib/api";
import { Input } from "../../../components/ui/input";
import { Switch } from "../../../components/ui/switch";
import { Textarea } from "../../../components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../../../components/ui/select";
import { cn } from "../../../lib/utils";
import { UNIT_SUFFIX, formatValue, toDraft, type DraftValue } from "../lib/values";
import { ProviderChainControl } from "./ProviderChainControl";

export function SettingControl({
  entry,
  value,
  onChange,
  invalid,
  describedBy,
}: {
  entry: SettingsCatalogEntry;
  value: DraftValue | undefined;
  onChange: (value: DraftValue) => void;
  invalid?: boolean;
  describedBy?: string;
}) {
  const id = `setting-${entry.key}`;
  const common = { id, "aria-invalid": invalid || undefined, "aria-describedby": describedBy };

  if (entry.input_type === "boolean") {
    return <Switch {...common} checked={value === true} onCheckedChange={onChange} aria-label={entry.label} />;
  }

  if (entry.input_type === "select") {
    return (
      // Radix Select inside a <form> emits "" while syncing its hidden native
      // select; every real option is non-empty, so "" is never a user choice.
      <Select value={String(value ?? "")} onValueChange={(next) => next && onChange(next)}>
        <SelectTrigger {...common} className="w-full sm:w-72">
          <SelectValue placeholder={`Default: ${formatValue(entry, entry.default)}`} />
        </SelectTrigger>
        <SelectContent>
          {(entry.options ?? []).map((option) => (
            <SelectItem key={option} value={option}>
              {entry.option_labels?.[option] ?? option}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    );
  }

  if (entry.input_type === "json") {
    return (
      <Textarea
        {...common}
        value={String(value ?? "")}
        onChange={(event) => onChange(event.target.value)}
        placeholder="{}"
        spellCheck={false}
        className={cn("min-h-28 w-full font-mono text-xs sm:w-96", invalid && "border-danger")}
      />
    );
  }

  if (entry.input_type === "provider_chain") {
    return <ProviderChainControl value={String(value ?? "")} onChange={onChange} />;
  }

  const placeholder = entry.placeholder ?? (entry.default !== undefined && entry.default !== "" ? formatValue(entry, entry.default) : undefined);

  if (entry.input_type === "number") {
    return (
      <div className="flex items-center gap-2">
        <Input
          {...common}
          inputMode="decimal"
          value={String(value ?? "")}
          onChange={(event) => onChange(event.target.value)}
          placeholder={entry.default === null || entry.default === undefined ? undefined : String(toDraft(entry, entry.default))}
          className={cn("w-32 text-right tabular-nums", invalid && "border-danger")}
        />
        {entry.unit ? <span className="w-10 text-sm text-text-muted">{UNIT_SUFFIX[entry.unit]}</span> : null}
      </div>
    );
  }

  return (
    <Input
      {...common}
      type={entry.input_type === "date" ? "date" : "text"}
      value={String(value ?? "")}
      onChange={(event) => onChange(event.target.value)}
      placeholder={placeholder}
      spellCheck={false}
      className={cn("w-full sm:w-80", invalid && "border-danger")}
    />
  );
}
