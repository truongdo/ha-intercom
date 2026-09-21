import { defineConfig } from "vite";

const backend = "http://127.0.0.1:8000";

export default defineConfig({
  base: "./",
  server: {
    proxy: {
      "/api": backend,
      "/login": backend,
      "/logout": backend,
      "/ws": { target: backend.replace("http", "ws"), ws: true },
    },
  },
});
