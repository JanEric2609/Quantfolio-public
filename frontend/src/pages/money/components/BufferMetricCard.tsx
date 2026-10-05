import { useQuery } from "@tanstack/react-query";
import { api } from "../../../lib/api";
import { formatCurrency } from "../../../lib/format";
import { Metric } from "../../../components/Metric";

interface BufferMetric {
  buffer_amount: number;
  avg_daily_spend: number;
  buffer_days: number | null;
}

export function BufferMetricCard() {
  const buffer = useQuery({
    queryKey: ["budget-buffer"],
    queryFn: () => api<BufferMetric>("/api/budget/buffer"),
  });

  const days = buffer.data?.buffer_days;
  const value = days == null ? formatCurrency(buffer.data?.buffer_amount) : `${Math.round(days)} days`;
  const tone = days == null ? "neutral" : days < 7 ? "bad" : days < 30 ? "warn" : "good";

  return (
    <Metric
      label="Buffer (days of spending ahead)"
      value={value}
      tone={tone}
    />
  );
}
