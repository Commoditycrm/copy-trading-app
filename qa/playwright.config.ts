import { defineConfig, devices } from "@playwright/test";

// Servers are managed outside Playwright (the QA harness starts them), so no
// `webServer` block — we just point at the running frontend. Override via env
// to run against a different local target; never point this at production.
const BASE_URL = process.env.E2E_BASE_URL || "http://localhost:3000";

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  expect: { timeout: 7_000 },
  // Auth flows share one backend/DB; keep it serial so parallel workers don't
  // race on the shared email-log capture. Revisit per-suite later.
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  use: {
    baseURL: BASE_URL,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    // firefox / webkit added in the cross-browser pass.
  ],
});
