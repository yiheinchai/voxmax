// Every user flow in the interface: turning blocking on and off, groups, applying changes,
// the preview, the allowlist, the activity log, and the failure paths for each.
const { test, expect } = require("@playwright/test");
const { openApp, commands } = require("./support/fake-engine");

const ON = { status: { active: true, watching: true, watcher_pid: 4242, applied_at: Date.now() / 1000 - 3600, remembered: 78, backend: "nftables", log_file: "/var/lib/hotspot-guard/watch.log", nat64_prefix: null } };
const STALE = { status: { active: true, watching: false, watcher_pid: null, applied_at: Date.now() / 1000 - 90000, remembered: 12, backend: "nftables", log_file: "/var/lib/hotspot-guard/watch.log", nat64_prefix: null } };

test.describe("turning blocking on and off", () => {
  test("starts with blocking off and the switch unchecked", async ({ page }) => {
    await openApp(page);
    await expect(page.locator("#hero-title")).toHaveText("Blocking is off");
    await expect(page.locator("#block-switch")).not.toBeChecked();
    await expect(page.locator("#watcher-value")).toHaveText("Not running");
    await expect(page.locator("#addresses-value")).toHaveText("0");
    await expect(page.locator("#applied-value")).toHaveText("Never");
  });

  test("turning blocking on starts the watcher and shows the on state", async ({ page }) => {
    await openApp(page);
    await page.locator("#block-switch").click();
    await expect(page.locator("#hero-title")).toHaveText("Blocking is on");
    await expect(page.locator("#block-switch")).toBeChecked();
    await expect(page.locator("#watcher-value")).toHaveText("Running (pid 4242)");
    await expect(page.locator("#addresses-value")).toHaveText("78");
    expect(await commands(page)).toContain("start_blocking");
  });

  test("while the watcher starts, the switch is held in place and the controls are disabled", async ({ page }) => {
    await openApp(page, { startDelayMs: 1500 });
    await page.locator("#block-switch").click();
    await expect(page.locator("#block-switch")).toBeDisabled();
    await expect(page.locator("#block-switch")).toBeChecked();
    await expect(page.locator("#switch-sub")).toContainText("Waiting for permission to start blocking");
    await expect(page.locator("#preview-btn")).toBeDisabled();
    await expect(page.locator("#block-switch")).toBeEnabled({ timeout: 10_000 });
    await expect(page.locator("#hero-title")).toHaveText("Blocking is on");
  });

  test("a watcher that fails to start raises an alert with its reason and reverts the switch", async ({ page }) => {
    await openApp(page, { startFailure: "pfctl: syntax error at line 4" });
    await page.locator("#block-switch").click();
    const alert = page.locator("#alert");
    await expect(alert).toBeVisible({ timeout: 20_000 });
    await expect(page.locator("#alert-text")).toContainText("The watcher did not start.");
    await expect(page.locator("#alert-text")).toContainText("pfctl: syntax error at line 4");
    await expect(page.locator("#block-switch")).not.toBeChecked();
    await page.locator("#alert-ok").click();
    await expect(alert).toBeHidden();
    await expect(page.locator("#hero-title")).toHaveText("Blocking is off");
  });

  test("dismissing the administrator prompt is quiet and reverts the switch", async ({ page }) => {
    await openApp(page, { startCancel: true, startDelayMs: 400 });
    await page.locator("#block-switch").click();
    await expect(page.locator("#block-switch")).toBeDisabled();
    await expect(page.locator("#block-switch")).toBeEnabled();
    await expect(page.locator("#alert")).toBeHidden();
    await expect(page.locator("#block-switch")).not.toBeChecked();
    await expect(page.locator("#hero-title")).toHaveText("Blocking is off");
  });

  test("turning blocking off stops the watcher and clears the rules", async ({ page }) => {
    await openApp(page, ON);
    await expect(page.locator("#block-switch")).toBeChecked();
    await page.locator("#block-switch").click();
    await expect(page.locator("#hero-title")).toHaveText("Blocking is off");
    await expect(page.locator("#watcher-value")).toHaveText("Not running");
    expect(await commands(page)).toContain("stop_blocking");
  });

  test("a cancelled stop keeps blocking on and reverts the switch without an alert", async ({ page }) => {
    await openApp(page, { ...ON, stopCancel: true });
    await page.locator("#block-switch").click();
    await expect(page.locator("#block-switch")).toBeEnabled();
    await expect(page.locator("#alert")).toBeHidden();
    await expect(page.locator("#block-switch")).toBeChecked();
    await expect(page.locator("#hero-title")).toHaveText("Blocking is on");
  });

  test("rules left behind by a dead watcher offer Clear Rules, which restores normal networking", async ({ page }) => {
    await openApp(page, STALE);
    await expect(page.locator("#hero-title")).toHaveText("Rules may still be active");
    await expect(page.locator("#stale-row")).toBeVisible();
    await expect(page.locator("#block-switch")).not.toBeChecked();
    await page.locator("#clear-btn").click();
    await expect(page.locator("#hero-title")).toHaveText("Blocking is off");
    await expect(page.locator("#stale-row")).toBeHidden();
    expect(await commands(page)).toContain("stop_blocking");
  });

  test("a watcher that dies while blocking is on is noticed on the next refresh", async ({ page }) => {
    await openApp(page, ON);
    await expect(page.locator("#hero-title")).toHaveText("Blocking is on");
    await page.evaluate(() => window.__fake.kill());
    await expect(page.locator("#hero-title")).toHaveText("Rules may still be active", { timeout: 8_000 });
    await expect(page.locator("#stale-row")).toBeVisible();
  });
});

test.describe("allowed groups", () => {
  test("turning a group on while blocking is off saves it and has nothing waiting to apply", async ({ page }) => {
    await openApp(page);
    await page.getByRole("button", { name: "Allowed Groups" }).click();
    await page.getByRole("checkbox", { name: "Dev" }).check();
    await expect(page.getByRole("checkbox", { name: "Dev" })).toBeChecked();
    expect(await page.evaluate(() => window.__fake.calls.filter((c) => c.command === "set_group").map((c) => c.args))).toEqual([
      { name: "dev", enabled: true },
    ]);
    await page.getByRole("button", { name: "Blocking" }).click();
    await expect(page.locator("#apply-row")).toBeHidden();
  });

  test("changing a group while blocking is on shows Changes waiting, and Apply Changes clears it", async ({ page }) => {
    await openApp(page, ON);
    await page.getByRole("button", { name: "Allowed Groups" }).click();
    await page.getByRole("checkbox", { name: "Custom" }).check();
    await page.getByRole("button", { name: "Blocking" }).click();
    await expect(page.locator("#apply-row")).toBeVisible();
    await expect(page.locator("#apply-row")).toContainText("Changes are waiting");
    await page.locator("#apply-btn").click();
    await expect(page.locator("#apply-row")).toBeHidden();
    expect(await commands(page)).toContain("apply_changes");
  });

  test("a cancelled Apply Changes keeps the changes waiting, without an alert", async ({ page }) => {
    await openApp(page, { ...ON, applyCancel: true });
    await page.getByRole("button", { name: "Allowed Groups" }).click();
    await page.getByRole("checkbox", { name: "Custom" }).check();
    await page.getByRole("button", { name: "Blocking" }).click();
    await page.locator("#apply-btn").click();
    await expect(page.locator("#apply-btn")).toBeEnabled();
    await expect(page.locator("#alert")).toBeHidden();
    await expect(page.locator("#apply-row")).toBeVisible();
  });

  test("a group that cannot be saved shows the reason and goes back to its saved state", async ({ page }) => {
    await openApp(page, { groupFailure: "the allowlist is read-only" });
    await page.getByRole("button", { name: "Allowed Groups" }).click();
    await page.getByRole("checkbox", { name: "Dev" }).click();
    await expect(page.locator("#alert")).toBeVisible();
    await expect(page.locator("#alert-text")).toHaveText("the allowlist is read-only");
    await page.keyboard.press("Escape");
    await expect(page.locator("#alert")).toBeHidden();
    await expect(page.getByRole("checkbox", { name: "Dev" })).not.toBeChecked();
  });

  test("each group names its switch and shows its entry count", async ({ page }) => {
    await openApp(page);
    await page.getByRole("button", { name: "Allowed Groups" }).click();
    await expect(page.getByRole("checkbox", { name: "Social" })).toBeChecked();
    await expect(page.getByRole("checkbox", { name: "Claude" })).toBeChecked();
    await expect(page.getByRole("checkbox", { name: "Dev" })).not.toBeChecked();
    await expect(page.locator(".group-row", { hasText: "Social" })).toContainText("38 entries");
    await expect(page.locator(".group-row", { hasText: "Custom" })).toContainText("0 entries");
  });
});

test.describe("preview and allowlist", () => {
  test("Preview lists the resolved addresses and calls out names with no address", async ({ page }) => {
    await openApp(page);
    await page.locator("#preview-btn").click();
    await expect(page.locator("#sheet")).toBeVisible();
    await expect(page.locator("#plan-list li")).toHaveCount(3);
    await expect(page.locator("#plan-list")).toContainText("api.anthropic.com");
    await expect(page.locator("#sheet-summary")).toHaveText("3 addresses resolved, 83 networks would be allowed.");
    await expect(page.locator("#plan-failed")).toContainText("No address for twimg.com. It stays blocked.");
    await page.locator("#sheet-done").click();
    await expect(page.locator("#sheet")).toBeHidden();
  });

  test("Escape closes the preview", async ({ page }) => {
    await openApp(page);
    await page.locator("#preview-btn").click();
    await expect(page.locator("#sheet")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.locator("#sheet")).toBeHidden();
  });

  test("a preview that fails shows the reason instead of an empty sheet", async ({ page }) => {
    await openApp(page, { planFailure: "could not resolve names: DNS is down" });
    await page.locator("#preview-btn").click();
    await expect(page.locator("#alert-text")).toHaveText("could not resolve names: DNS is down");
    await expect(page.locator("#sheet")).toBeHidden();
  });

  test("Edit Allowlist asks the shell to open the file", async ({ page }) => {
    await openApp(page);
    await page.getByRole("button", { name: "Edit Allowlist" }).click();
    expect(await commands(page)).toContain("open_allowlist");
    await expect(page.locator("#alert")).toBeHidden();
  });

  test("Edit Allowlist shows an error when the editor cannot be opened", async ({ page }) => {
    await openApp(page, { openFailure: "no editor is set up for .ini files" });
    await page.getByRole("button", { name: "Edit Allowlist" }).click();
    await expect(page.locator("#alert-text")).toHaveText("no editor is set up for .ini files");
  });
});

test.describe("navigation and the activity log", () => {
  test("the sidebar switches panes, the title follows, and the current item is marked", async ({ page }) => {
    await openApp(page);
    await expect(page.locator("#pane-title")).toHaveText("Blocking");
    await page.getByRole("button", { name: "Allowed Groups" }).click();
    await expect(page.locator("#pane-title")).toHaveText("Allowed Groups");
    await expect(page.locator("#pane-allowed")).toBeVisible();
    await expect(page.locator("#pane-blocking")).toBeHidden();
    await expect(page.getByRole("button", { name: "Allowed Groups" })).toHaveAttribute("aria-current", "page");
    await page.getByRole("button", { name: "Activity" }).click();
    await expect(page.locator("#pane-title")).toHaveText("Activity");
    await expect(page.getByRole("button", { name: "Blocking" })).not.toHaveAttribute("aria-current", "page");
  });

  test("the activity pane shows the watcher log", async ({ page }) => {
    await openApp(page, { ...ON, log: ["21:10:01 blocking on via nftables", "21:15:01 refreshed: 83 networks allowed"] });
    await page.getByRole("button", { name: "Activity" }).click();
    await expect(page.locator("#log")).toContainText("blocking on via nftables");
    await expect(page.locator("#log")).toContainText("refreshed: 83 networks allowed");
  });

  test("the activity pane says so when nothing has been logged", async ({ page }) => {
    await openApp(page);
    await page.getByRole("button", { name: "Activity" }).click();
    await expect(page.locator("#log")).toContainText("No activity yet");
  });

  test("an engine that cannot be read shows a banner, and the banner clears when it recovers", async ({ page }) => {
    await openApp(page);
    await page.evaluate(() => window.__fake.set({ statusFailure: "the engine is not responding" }));
    await expect(page.locator("#banner")).toBeVisible({ timeout: 8_000 });
    await expect(page.locator("#banner")).toHaveText("the engine is not responding");
    await page.evaluate(() => window.__fake.set({ statusFailure: null }));
    await expect(page.locator("#banner")).toBeHidden({ timeout: 8_000 });
  });
});

test.describe("keyboard and dialogs", () => {
  test("the blocking switch can be turned on from the keyboard", async ({ page }) => {
    await openApp(page);
    await page.locator("#block-switch").focus();
    await page.keyboard.press("Space");
    await expect(page.locator("#hero-title")).toHaveText("Blocking is on");
  });

  test("opening an alert moves focus to its OK button, and Enter dismisses it", async ({ page }) => {
    await openApp(page, { groupFailure: "nope" });
    await page.getByRole("button", { name: "Allowed Groups" }).click();
    await page.getByRole("checkbox", { name: "Dev" }).click();
    await expect(page.locator("#alert-ok")).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page.locator("#alert")).toBeHidden();
  });
});
