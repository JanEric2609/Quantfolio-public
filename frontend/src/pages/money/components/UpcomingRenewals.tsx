import { Card, CardContent, CardHeader, CardTitle } from "../../../components/ui/card";
import { Badge } from "../../../components/ui/badge";
import { formatCurrency, formatDate } from "../../../lib/format";

interface RenewalItem {
  id: string;
  name: string;
  amount: string;
  currency: string;
  next_due_date: string;
}

export function UpcomingRenewals({ items }: { items: RenewalItem[] }) {
  if (items.length === 0) return null;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm font-medium">Upcoming Renewals</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        {items.map((item) => {
          const due = new Date(item.next_due_date);
          const days = Math.ceil((due.getTime() - Date.now()) / (1000 * 60 * 60 * 24));
          return (
            <div key={item.id} className="flex items-center justify-between rounded-md border border-border bg-surface-2 p-3 text-sm">
              <div>
                <div className="font-medium">{item.name}</div>
                <div className="text-xs text-text-muted">{formatDate(item.next_due_date)}</div>
              </div>
              <div className="flex items-center gap-2">
                <span className="tabular-nums">{formatCurrency(Number(item.amount), { currency: item.currency })}</span>
                {days <= 3 && <Badge variant="danger" className="text-xs">{days <= 0 ? "Overdue" : `${days}d`}</Badge>}
                {days > 3 && days <= 14 && <Badge variant="warning" className="text-xs">{days}d</Badge>}
              </div>
            </div>
          );
        })}
      </CardContent>
    </Card>
  );
}
