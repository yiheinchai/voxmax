// Appearance and layout: light and dark, the macOS treatment, the smallest window size,
// keyboard focus, reduced motion, and the status details shown in the sidebar and the Status list.
const { test, expect } = require("@playwright/test");
const { openApp } = require("./support/fake-engine");

const ON = { status: { active: true, watching: true, watcher_pid: 4242, applied_at: Date.now() / 1000 - 3600, remembered: 78, backend: "nftables", log_file: "/var/lib/hotspot-guard/watch.log", nat64_prefix: "64:ff9b::/96" } };

async function bodyBackground(page) {
  return page.evaluate(() => getComputedStyle(document.body).backgroundColor);
}

test.describe("appearance", () => {
  test("light appearance uses the light content background", async ({ page }) => {
    await page.emulateMedia({ colorScheme: "light" });
    await openApp(page);
    expect(await bodyBackground(page)).toBe("rgb(245, 245, 247)");
  });

  test("dark appearance uses the dark content background", async ({ page }) => {
    await page.emulateMedia({ colorScheme: "dark" });
    await openApp(page);
    expect(await bodyBackground(page)).toBe("rgb(30, 30, 32)");
    await expect(page.locator("#block-switch")).not.toBeChecked();
    await expect(page.locator(".grouped").first()).toBeVisible();
  });

  test("macOS gets a transparent sidebar for the system material and room for the window buttons", async ({ page }) => {
    await openApp(page, { platform: "macos" });
    await expect(page.locator("html")).toHaveAttribute("data-platform", "macos");
    const sidebar = await page.evaluate(() => getComputedStyle(document.querySelector(".sidebar")).backgroundColor);
    expect(sidebar).toBe("rgba(0, 0, 0, 0)");
    const chrome = await page.evaluate(() => document.querySelector(".sidebar-chrome").getBoundingClientRect().height);
    expect(chrome).toBeGreaterThanOrEqual(40);
  });

  test("Windows and Linux keep a solid sidebar with their own title bar", async ({ page }) => {
    await openApp(page, { platform: "windows" });
    await expect(page.locator("html")).toHaveAttribute("data-platform", "windows");
    const sidebar = await page.evaluate(() => getComputedStyle(document.querySelector(".sidebar")).backgroundColor);
    expect(sidebar).not.toBe("rgba(0, 0, 0, 0)");
  });

  test("reduced motion removes the switch animation", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await openApp(page);
    const duration = await page.evaluate(() => getComputedStyle(document.querySelector(".switch .thumb")).transitionDuration);
    expect(parseFloat(duration)).toBeLessThan(0.001);  // 0.01ms, reported as seconds
  });
});

test.describe("layout", () => {
  test("the smallest window fits without horizontal scrolling", async ({ page }) => {
    await page.setViewportSize({ width: 760, height: 460 });
    await openApp(page, ON);
    const fits = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
    expect(fits).toBe(true);
    const overflowing = await page.evaluate(() => {
      const width = window.innerWidth;
      return [...document.querySelectorAll(".row, .toolbar, .hero, .sidebar-item")]
        .filter((el) => el.getBoundingClientRect().right > width + 1)
        .map((el) => el.className);
    });
    expect(overflowing).toEqual([]);
    await expect(page.getByRole("button", { name: "Preview" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Edit Allowlist" })).toBeVisible();
    await expect(page.locator("#block-switch")).toBeVisible();
  });

  test("the alert fits a long error message at the smallest size", async ({ page }) => {
    await page.setViewportSize({ width: 760, height: 460 });
    await openApp(page, { startFailure: "pfctl: " + "very long error line ".repeat(20) });
    await page.locator("#block-switch").click();
    await expect(page.locator("#alert")).toBeVisible({ timeout: 20_000 });
    const box = await page.locator(".alert").boundingBox();
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(760);
  });

  test("the preview sheet stays inside the window", async ({ page }) => {
    await page.setViewportSize({ width: 760, height: 460 });
    await openApp(page);
    await page.locator("#preview-btn").click();
    const box = await page.locator(".sheet").boundingBox();
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(760);
    expect(box.y + box.height).toBeLessThanOrEqual(460);
  });
});

test.describe("focus and the status details", () => {
  test("keyboard focus shows a visible ring on the blocking switch", async ({ page }) => {
    await openApp(page);
    for (let i = 0; i < 12; i += 1) {
      await page.keyboard.press("Tab");
      if (await page.evaluate(() => document.activeElement && document.activeElement.id === "block-switch")) break;
    }
    await expect(page.locator("#block-switch")).toBeFocused();
    const outline = await page.evaluate(() => getComputedStyle(document.querySelector(".switch .track")).outlineStyle);
    expect(outline).not.toBe("none");
  });

  test("the sidebar footer reports the blocking state", async ({ page }) => {
    await openApp(page);
    await expect(page.locator("#sidebar-status")).toHaveText("Blocking off");
    await expect(page.locator("#sidebar-dot")).not.toHaveClass(/is-on/);
    await page.locator("#block-switch").click();
    await expect(page.locator("#sidebar-status")).toHaveText("Blocking on");
    await expect(page.locator("#sidebar-dot")).toHaveClass(/is-on/);
  });

  test("the NAT64 row shows the translation prefix when the network uses one", async ({ page }) => {
    await openApp(page, ON);
    await expect(page.locator("#nat64-value")).toHaveText("64:ff9b::/96");
  });

  test("the NAT64 row says so when blocking is on without a translation prefix", async ({ page }) => {
    await openApp(page, { status: { ...ON.status, nat64_prefix: null } });
    await expect(page.locator("#nat64-value")).toHaveText("Not used");
  });

  test("the NAT64 row is blank while blocking is off", async ({ page }) => {
    await openApp(page);
    await expect(page.locator("#nat64-value")).toHaveText("—");
  });

  test("an allowlist with no groups says how to add one", async ({ page }) => {
    await openApp(page, { groups: [] });
    await page.getByRole("button", { name: "Allowed Groups" }).click();
    await expect(page.locator("#groups-list")).toContainText("No groups yet");
  });
});
