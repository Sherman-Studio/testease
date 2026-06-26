import { defineConfig, devices } from '@playwright/test'

// Test Ease Playwright config. Spawns Vite dev server (the SPA) and runs
// against http://localhost:5173. The API is fully mocked per-test via
// page.route('/api/**'), so no FastAPI / Mongo is required — these are
// fast smoke tests that exercise the SPA's wiring, not the backend.
//
// To add an integration tier that hits a real backend, declare a second
// project with its own `webServer` array spinning up uvicorn + Mongo.
// Out of scope for the initial setup.
export default defineConfig({
  testDir: './tests',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    baseURL: 'http://localhost:5173',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  // Spawn the SPA dev server. The `cwd` is the SPA root; reuseExistingServer
  // means a running `npm run dev` is reused locally (no surprise restart).
  webServer: {
    command: 'npm run dev -- --port 5173 --strictPort',
    cwd: '../web',
    url: 'http://localhost:5173',
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
  },
})
