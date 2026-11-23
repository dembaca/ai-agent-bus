import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Served from https://dembaca.github.io/ai-agent-bus/ (GitHub Pages).
export default defineConfig({
  base: '/ai-agent-bus/',
  plugins: [react()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // Two pages: what a release can do (index.html) and the plan (target.html),
    // relative to the site root, where the npm scripts run.
    rollupOptions: { input: { main: 'index.html', target: 'target.html' } },
  },
})
