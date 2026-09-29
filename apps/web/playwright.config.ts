import { defineConfig } from "@playwright/test";

// Runs against an already running stack (`make up`, or `make api` + `make worker` locally).
export default defineConfig({
  testDir: "./e2e",
  timeout: 8 * 60_000,
  expect: { timeout: 20_000 },
  retries: 0,
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:8000",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    launchOptions: process.env.PW_CHROMIUM_PATH ? { executablePath: process.env.PW_CHROMIUM_PATH } : {},
  },
});
