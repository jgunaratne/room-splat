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
  // Emit the app bundle under /static/, NOT /assets/: the server mounts /assets for
  // versioned cell + point-cloud data (manifest URLs), which would otherwise shadow the
  // bundle and 404 the whole viewer in a browser.
  build: { outDir: "dist", assetsDir: "static" },
});
