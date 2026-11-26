import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"

// The operator console's browser app (ADR-015 point 14: React with
// TypeScript, built with Vite, as site/ already is). Not published anywhere:
// the built bundle is served by the console server later; nothing to set
// in `base`. The app talks to a mock of the console API unless
// VITE_CB_CONSOLE_API names a real server.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  // Development against a local console server (the login's cookie is
  // same-origin, so the API is proxied, never cross-origin):
  //   server: { proxy: { "/v1": "http://127.0.0.1:8747" } },
  // with `VITE_CB_CONSOLE_API=""` — the same shapes the built app serves.
})
