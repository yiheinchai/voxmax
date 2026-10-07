// Browser end-to-end tests for the Hotspot Guard interface.
// The UI runs in Chromium against a stateful stand-in for the Tauri bridge (tests/support/fake-engine.js),
// so every flow can be driven and checked without elevated rights or a firewall.
const fs = require("fs");
const { defineConfig, devices } = require("@playwright/test");

// The sandbox ships a Chromium build; elsewhere Playwright uses its own browser.
const SANDBOX_CHROMIUM = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome";
const executablePath = process.env.HG_CHROMIUM || (fs.existsSync(SANDBOX_CHROMIUM) ? SANDBOX_CHROMIUM : undefined);
const asRoot = typeof process.getuid === "function" && process.getuid() === 0;

module.exports = defineConfig({
  testDir: "./tests",
  timeout: 45_000,
  expect: { timeout: 6_000 },
  fullyParallel: true,
  reporter: [["list"]],
  use: {
    baseURL: "http://127.0.0.1:8765",
    trace: "retain-on-failure",
    launchOptions: {
      executablePath,
      args: asRoot ? ["--no-sandbox"] : [],
    },
  },
  webServer: {
    command: "python3 -m http.server 8765 --bind 127.0.0.1 --directory ../ui",
    url: "http://127.0.0.1:8765/index.html",
    reuseExistingServer: true,
    stdout: "ignore",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"], launchOptions: { executablePath, args: asRoot ? ["--no-sandbox"] : [] } } },
  ],
});
