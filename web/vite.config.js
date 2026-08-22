import { defineConfig } from "vite";

// Single origin in production: the FastAPI server hosts the built bundle and both
// WebSockets (SPEC.md M3). In dev we proxy the WS + assets to the server so there is
// still no CORS anywhere.
export default defineConfig({
  server: {
    proxy: {
      "/ws": { target: "ws://localhost:8000", ws: true },
      "/assets": "http://localhost:8000",
    },
  },
  build: { outDir: "dist" },
});
