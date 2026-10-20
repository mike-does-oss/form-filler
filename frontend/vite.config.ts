import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The FastAPI backend serves dist/ (assets at /assets). In dev, /api is proxied to it.
declare const process: { env: Record<string, string | undefined> };
const backend = process.env.FF_BACKEND ?? "http://127.0.0.1:8080";

export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", assetsDir: "assets", sourcemap: false, chunkSizeWarningLimit: 600 },
  server: {
    port: 5173,
    proxy: { "/api": { target: backend, changeOrigin: false } },
  },
});
