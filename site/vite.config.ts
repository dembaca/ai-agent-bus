import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Served from https://dembaca.github.io/ai-agent-bus/ (GitHub Pages).
export default defineConfig({
  base: '/ai-agent-bus/',
  plugins: [react()],
  build: { outDir: 'dist', emptyOutDir: true },
})
