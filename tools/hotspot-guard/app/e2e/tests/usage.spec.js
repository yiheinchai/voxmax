// The Data Usage page: all traffic and excluding social media, the two periods, and what the
// page says when the split is unavailable, nothing has been recorded, or the engine fails.
const { test, expect } = require("@playwright/test");
const { openApp, installFakeEngine, commands } = require("./support/fake-engine");

async function openUsage(page, overrides = {}) {
  await openApp(page, overrides);
  await page.locator('.sidebar-item[data-pane="usage"]').click();
  await expect(page.locator("#pane-title")).toHaveText("Data Usage");
  await expect(page.locator("#usage-headline")).not.toHaveText("—");
}

async function calls(page, command) {
  return page.evaluate((name) => window.__fake.calls.filter((c) => c.command === name).map((c) => c.args), command);
}

test.describe("data usage", () => {
  test("shows all traffic, with social media and everything else split out", async ({ page }) => {
    await openUsage(page);
    await expect(page.locator("#usage-headline-label")).toHaveText("All traffic");
    await expect(page.locator("#usage-social")).not.toHaveText("—");
    await expect(page.locator("#usage-other")).not.toHaveText("—");
    await expect(page.locator("#usage-social-sub")).toContainText("% of measured traffic");
    await expect(page.locator("#usage-chart rect.bar-social")).toHaveCount(24);
    await expect(page.locator("#usage-chart rect.bar-other")).toHaveCount(24);
  });

  test("excluding social media shows only everything else, and the chart drops the social bars", async ({ page }) => {
    await openUsage(page);
    const allTotal = await page.locator("#usage-headline").textContent();
    await page.getByRole("radio", { name: "Excluding social media" }).click();
    await expect(page.locator("#usage-headline-label")).toHaveText("Excluding social media");
    await expect(page.locator("#usage-headline")).toHaveText(await page.locator("#usage-other").textContent());
    await expect(page.locator("#usage-headline")).not.toHaveText(allTotal);
    await expect(page.locator("#usage-chart rect.bar-social")).toHaveCount(0);
    await expect(page.locator("#usage-chart rect.bar-other")).toHaveCount(24);
    await expect(page.getByRole("radio", { name: "Excluding social media" })).toHaveAttribute("aria-checked", "true");
  });

  test("the 7-day period asks for daily buckets and shows seven bars", async ({ page }) => {
    await openUsage(page);
    await page.getByRole("radio", { name: "7 days" }).click();
    await expect(page.locator("#usage-chart rect.bar-other")).toHaveCount(7);
    expect(await calls(page, "engine_usage")).toContainEqual({ hours: 168, bucket: 86400 });
    await expect(page.locator("#usage-headline-sub")).toHaveText("in the last 7 days");
  });

  test("when the system has no per-connection counters, the split says so instead of guessing", async ({ page }) => {
    await openUsage(page, { usageMode: "unsplit" });
    await expect(page.locator("#usage-social")).toHaveText("—");
    await expect(page.locator("#usage-other")).toHaveText("—");
    await expect(page.locator("#usage-social-sub")).toHaveText("Not available on this system");
    await expect(page.locator("#usage-note")).toContainText("does not provide");
    await page.getByRole("radio", { name: "Excluding social media" }).click();
    await expect(page.locator("#usage-headline")).toHaveText("—");
    await expect(page.locator("#usage-headline-sub")).toHaveText("Not available on this system");
  });

  test("before anything is recorded, the page says how recording works", async ({ page }) => {
    await openUsage(page, { usageMode: "empty", tracking: false });
    await expect(page.locator("#usage-note")).toContainText("No usage recorded yet");
    await expect(page.locator("#usage-note")).toContainText("Blocking is off");
    await expect(page.locator("#usage-chart rect")).toHaveCount(0);
  });

  test("an engine failure shows the reason on the page, without an alert", async ({ page }) => {
    await openApp(page, { usageFailure: "usage history is unreadable" });
    await page.locator('.sidebar-item[data-pane="usage"]').click();
    await expect(page.locator("#usage-note")).toHaveText("Usage could not be read: usage history is unreadable");
    await expect(page.locator("#alert")).toBeHidden();
  });

  test("the page follows the engine, so a new sample shows up on the next refresh", async ({ page }) => {
    await openUsage(page, { usageMode: "empty", tracking: false });
    await expect(page.locator("#usage-note")).toContainText("No usage recorded yet");
    await page.evaluate(() => window.__fake.set({ usageMode: "split", tracking: true }));
    await expect(page.locator("#usage-chart rect.bar-other")).toHaveCount(24, { timeout: 8_000 });
    await expect(page.locator("#usage-note")).not.toContainText("No usage recorded yet");
  });

  test("the usage page can be reached from the keyboard", async ({ page }) => {
    await openApp(page);
    await page.getByRole("button", { name: "Data Usage" }).focus();
    await page.keyboard.press("Enter");
    await expect(page.locator("#pane-usage")).toBeVisible();
    await expect(page.getByRole("button", { name: "Data Usage" })).toHaveAttribute("aria-current", "page");
  });
});
