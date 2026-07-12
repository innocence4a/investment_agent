import { defineConfig } from "vite";

// 開発時: Vite dev server から core(:8765)へ /ws・/api をプロキシする
export default defineConfig({
  server: {
    proxy: {
      "/ws": { target: "ws://127.0.0.1:8765", ws: true },
      "/api": { target: "http://127.0.0.1:8765" },
    },
  },
});
