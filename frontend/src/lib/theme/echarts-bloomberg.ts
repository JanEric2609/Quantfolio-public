import { echarts } from "../echarts";

const PALETTE = ["#FA8001", "#22D3EE", "#22c55e", "#ef4444", "#a78bfa", "#facc15", "#f472b6", "#60a5fa"];

echarts.registerTheme("bloomberg", {
  color: PALETTE,
  backgroundColor: "transparent",
  textStyle: { fontFamily: "Inter, sans-serif", color: "#b3b3b3" },
  title: { textStyle: { color: "#fafafa", fontWeight: 600 } },
  grid: { top: 32, left: 48, right: 24, bottom: 40, borderColor: "#262626", containLabel: true },
  legend: { textStyle: { color: "#b3b3b3" } },
  xAxis: {
    axisLine:  { lineStyle: { color: "#262626" } },
    axisLabel: { color: "#b3b3b3", fontFamily: "JetBrains Mono, ui-monospace, monospace" },
    splitLine: { lineStyle: { color: "#1a1a1a" } },
  },
  yAxis: {
    axisLine:  { lineStyle: { color: "#262626" } },
    axisLabel: { color: "#b3b3b3", fontFamily: "JetBrains Mono, ui-monospace, monospace" },
    splitLine: { lineStyle: { color: "#1a1a1a" } },
  },
  tooltip: {
    backgroundColor: "#181818",
    borderColor: "#3a3a3a",
    borderWidth: 1,
    textStyle: { color: "#fafafa", fontFamily: "JetBrains Mono, ui-monospace, monospace", fontSize: 12 },
  },
});

echarts.registerTheme("bloomberg-light", {
  color: PALETTE,
  backgroundColor: "transparent",
  textStyle: { fontFamily: "Inter, sans-serif", color: "#666666" },
  title: { textStyle: { color: "#1a1a1a", fontWeight: 600 } },
  grid: { top: 32, left: 48, right: 24, bottom: 40, borderColor: "#e0e0e0", containLabel: true },
  legend: { textStyle: { color: "#666666" } },
  xAxis: {
    axisLine:  { lineStyle: { color: "#e0e0e0" } },
    axisLabel: { color: "#666666", fontFamily: "JetBrains Mono, ui-monospace, monospace" },
    splitLine: { lineStyle: { color: "#f0f0f0" } },
  },
  yAxis: {
    axisLine:  { lineStyle: { color: "#e0e0e0" } },
    axisLabel: { color: "#666666", fontFamily: "JetBrains Mono, ui-monospace, monospace" },
    splitLine: { lineStyle: { color: "#f0f0f0" } },
  },
  tooltip: {
    backgroundColor: "#ffffff",
    borderColor: "#e0e0e0",
    borderWidth: 1,
    textStyle: { color: "#1a1a1a", fontFamily: "JetBrains Mono, ui-monospace, monospace", fontSize: 12 },
  },
});
