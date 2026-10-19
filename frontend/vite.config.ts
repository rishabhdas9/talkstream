import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The API base URL defaults to the page origin (src/api/endpoints.ts), so the
// dev server proxies API and voice-WebSocket routes to the backend.
const BACKEND = "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/auth": BACKEND,
      "/wallet": BACKEND,
      "/analytics": BACKEND,
      "/capabilities": BACKEND,
      "/knowledge": BACKEND,
      "/operations": BACKEND,
      "/health": BACKEND,
      "/ws": { target: BACKEND, ws: true },
    },
  },
});
