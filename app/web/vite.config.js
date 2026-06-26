/// <reference types="vitest" />
import { fileURLToPath, URL } from 'node:url'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// The SPA is built (`vite build` → dist/) and the FastAPI app serves dist/.
// In local dev, `vite dev` proxies /api to a separately-run uvicorn so the
// two halves can iterate independently.
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
  build: {
    outDir: 'dist',
  },
  test: {
    // Vitest config — happy-dom is lighter than jsdom and plenty for the
    // shallow component tests under src/**/__tests__/. Tests sit alongside
    // their subject (Avatar.vue → Avatar.test.js etc.) so a refactor that
    // moves a component carries its tests with it.
    environment: 'happy-dom',
    globals: true,
    include: ['src/**/*.{test,spec}.{js,ts}'],
    setupFiles: ['./tests/setup.js'],
  },
})
