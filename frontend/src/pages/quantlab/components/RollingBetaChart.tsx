import { LineChart } from "../../../components/charts/LineChart";

export function RollingBetaChart({
  timeline = [], name = "Rolling beta", yName = "Beta (63 trading days)",
}: { timeline?: Array<{ date: string; value: number }>; name?: string; yName?: string }) {
  return (
    <LineChart
      ariaLabel={name}
      className="h-72"
      series={[{ name, data: timeline.map((item) => [item.date, item.value]) }]}
      yName={yName}
      yFormat={(v) => v.toFixed(2)}
    />
  );
}
