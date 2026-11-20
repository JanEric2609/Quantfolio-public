export default function ReactECharts({ option }: { option: unknown }) {
  return <div data-testid="echart-stub" data-option={JSON.stringify(option)} />;
}
