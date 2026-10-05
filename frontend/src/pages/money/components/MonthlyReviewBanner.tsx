import { useState } from "react";
import { X } from "lucide-react";
import { Button } from "../../../components/ui/button";
import { MonthlyReviewDialog } from "./MonthlyReviewDialog";

const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

function dismissKey(year: number, month: number) {
  return `budget-review-dismissed-${year}-${month}`;
}

export function MonthlyReviewBanner() {
  const today = new Date();
  const targetYear = today.getFullYear();
  const targetMonth = today.getMonth() + 1;
  const [reviewYear, reviewMonth] = targetMonth === 1 ? [targetYear - 1, 12] : [targetYear, targetMonth - 1];

  const key = dismissKey(reviewYear, reviewMonth);
  const [dismissed, setDismissed] = useState(() => localStorage.getItem(key) === "1");
  const [dialogOpen, setDialogOpen] = useState(false);

  // Only surface the prompt in the first week of a new month — never a hard
  // gate, rollover already happened automatically regardless.
  if (dismissed || today.getDate() > 7) return null;

  const dismiss = () => {
    localStorage.setItem(key, "1");
    setDismissed(true);
  };

  return (
    <>
      <div className="flex items-center justify-between gap-3 rounded-lg border border-border bg-surface-2 p-4 text-sm">
        <span>
          Review <span className="font-medium">{MONTH_NAMES[reviewMonth - 1]}</span>? Adjust targets, clear up
          uncategorized spend, and check in on sinking funds.
        </span>
        <div className="flex items-center gap-2 shrink-0">
          <Button size="sm" onClick={() => setDialogOpen(true)}>
            Review
          </Button>
          <Button size="sm" variant="ghost" className="h-11 w-11 p-0 sm:h-7 sm:w-7" aria-label="Dismiss review prompt" onClick={dismiss}>
            <X className="h-4 w-4" />
          </Button>
        </div>
      </div>
      <MonthlyReviewDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        reviewYear={reviewYear}
        reviewMonth={reviewMonth}
        targetYear={targetYear}
        targetMonth={targetMonth}
      />
    </>
  );
}
