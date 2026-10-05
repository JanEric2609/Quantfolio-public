import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";
import { API_CACHE_NAME, API_READ_CACHE_PATTERN } from "./src/lib/pwa.ts";

export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: "autoUpdate",
      includeAssets: ["icons/apple-touch-icon.png"],
      workbox: {
        // A navigation to an API path must reach the server, not the app shell.
        navigateFallbackDenylist: [/^\/api\//, /^\/health/],
        runtimeCaching: [
          {
            // Plan, wealth and holdings: the network first (4 s), the last good copy when
            // offline or slow. GET only, and only these paths; see API_READ_CACHE_PATTERN.
            urlPattern: API_READ_CACHE_PATTERN,
            method: "GET",
            handler: "NetworkFirst",
            options: {
              cacheName: API_CACHE_NAME,
              networkTimeoutSeconds: 4,
              expiration: { maxEntries: 20, maxAgeSeconds: 7 * 24 * 60 * 60 },
              cacheableResponse: { statuses: [200] },
            },
          },
        ],
      },
      manifest: {
        // A stable identity: installs made when start_url was "/" keep resolving to this id.
        id: "/",
        name: "Quantfolio",
        short_name: "Quantfolio",
        description: "Self-hosted portfolio, tax, and budget cockpit",
        theme_color: "#0a0a0a",
        background_color: "#0a0a0a",
        display: "standalone",
        categories: ["finance"],
        start_url: "/plan",
        scope: "/",
        icons: [
          { src: "icons/icon-192.png", sizes: "192x192", type: "image/png" },
          { src: "icons/icon-512.png", sizes: "512x512", type: "image/png" },
          { src: "icons/icon-512-maskable.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
        ],
        shortcuts: [
          { name: "This month", short_name: "This month", url: "/plan", icons: [{ src: "icons/icon-192.png", sizes: "192x192", type: "image/png" }] },
          { name: "Overview", short_name: "Overview", url: "/", icons: [{ src: "icons/icon-192.png", sizes: "192x192", type: "image/png" }] },
          { name: "Decide", short_name: "Decide", url: "/decide", icons: [{ src: "icons/icon-192.png", sizes: "192x192", type: "image/png" }] },
        ],
      },
    }),
  ],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/health": "http://127.0.0.1:8000",
    },
  },
  build: {
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            { name: "radix", test: /@radix-ui\// },
            { name: "recharts", test: /recharts/ },
            { name: "zrender", test: /zrender/ },
          ],
        },
      },
    },
  },
});
