import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: [
      {
        find: /^echarts-for-react\/lib\/core$/,
        replacement: fileURLToPath(new URL("./src/test/mocks/echarts.tsx", import.meta.url)),
      },
      {
        find: /^echarts-for-react$/,
        replacement: fileURLToPath(new URL("./src/test/mocks/echarts.tsx", import.meta.url)),
      },
    ],
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    globals: true,
    testTimeout: 20000,
    coverage: {
      provider: "v8",
      reporter: ["text", "html"],
      include: ["src/**/*.{ts,tsx}"],
      exclude: [
        "src/test/**",
        "src/**/*.d.ts",
        "src/**/index.ts",
        "src/vite-env.d.ts",
      ],
    },
  },
});
