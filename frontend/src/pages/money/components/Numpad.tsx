import { Delete } from "lucide-react";
import { Button } from "../../../components/ui/button";
import { formatCurrency } from "../../../lib/format";

const KEYS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "", "0", "back"] as const;

/** The cents-first buffer as a German euro amount: "350" -> "3,50 €". */
export function formatRawAmount(raw: string): string {
  return formatCurrency(rawToAmount(raw), "EUR");
}

export function rawToAmount(raw: string): number {
  return raw ? Number(raw) / 100 : 0;
}

const MAX_DIGITS = 9;

export function Numpad({ value, onChange }: { value: string; onChange: (next: string) => void }) {
  const press = (key: string) => {
    if (key === "") return;
    if (key === "back") {
      onChange(value.slice(0, -1));
      return;
    }
    if (value.length >= MAX_DIGITS) return;
    const next = (value + key).replace(/^0+(?=\d)/, "");
    onChange(next);
  };

  return (
    <div className="grid grid-cols-3 gap-2" role="group" aria-label="Amount numpad">
      {KEYS.map((key, i) =>
        key === "" ? (
          <div key={i} />
        ) : (
          <Button
            key={i}
            type="button"
            variant="outline"
            size="icon"
            className="h-16 w-full text-xl"
            aria-label={key === "back" ? "Backspace" : `Digit ${key}`}
            onClick={() => press(key)}
          >
            {key === "back" ? <Delete className="h-5 w-5" /> : key}
          </Button>
        )
      )}
    </div>
  );
}
