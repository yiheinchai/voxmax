// End-to-end tests for the real Hotspot Guard app. They drive the built binary through
// tauri-driver and check the side effects on disk and in the firewall, not just the screen.
//
// Run through run-tests.sh, which puts everything in an isolated network namespace. The app's
// blocking then changes only that namespace's firewall, and the app's config goes to a temporary
// HOME. Nothing on the host is touched.
import { after, before, test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Builder, By, until } from "selenium-webdriver";

const here = path.dirname(fileURLToPath(import.meta.url));
const APP = path.resolve(process.env.HG_APP_BINARY ?? path.join(here, "../../src-tauri/target/debug/hotspot-guard-app"));
const DRIVER = process.env.HG_DRIVER ?? "http://127.0.0.1:4444/";
const ENGINE = process.env.HG_ENGINE ?? path.join(here, "../../../hotspot_guard.py");
const DATA_HOME = process.env.XDG_DATA_HOME;
const CONFIG = path.join(DATA_HOME, "local.hotspot-guard", "hotspot-guard.ini");
const XDG_OPEN = ["/usr/bin/xdg-open", "/bin/xdg-open"].some((p) => existsSync(p));

let driver;

function enginePath() {
  return ENGINE;
}

/** Text content of an element by id, even when its pane is hidden. */
async function read(id) {
  return driver.executeScript((elementId) => document.getElementById(elementId)?.textContent ?? "", id);
}

async function waitForText(id, expected, timeout = 30_000) {
  await driver.wait(async () => (await read(id)).trim() === expected, timeout, `#${id} never showed "${expected}"`);
}

async function waitForVisible(id, timeout = 30_000) {
  await driver.wait(until.elementIsVisible(await driver.findElement(By.id(id))), timeout, `#${id} never appeared`);
}

async function waitForHidden(id, timeout = 30_000) {
  await driver.wait(async () => !(await driver.executeScript((elementId) => {
    const el = document.getElementById(elementId);
    return el !== null && !el.hidden;
  }, id)), timeout, `#${id} never hid`);
}

/** Clicks a control once it is enabled. A disabled control (busy with an action) ignores clicks. */
async function click(selector) {
  await driver.wait(async () => driver.executeScript((s) => {
    const el = document.querySelector(s);
    return el !== null && !el.disabled;
  }, selector), 60_000, `${selector} stayed disabled`);
  await driver.findElement(By.css(selector)).click();
}

/** Turns a group on or off through the UI, and waits until the app has saved and shown it. */
async function setGroup(name, enabled) {
  await click(`input[data-group="${name}"]`);
  await driver.wait(async () => groupEnabled(name) === enabled, 15_000,
    `${name} was not saved as ${enabled ? "enabled" : "disabled"}`);
  await driver.wait(async () => driver.executeScript((s, want) => document.querySelector(s)?.checked === want,
    `input[data-group="${name}"]`, enabled), 15_000, `${name} did not show as ${enabled ? "on" : "off"}`);
}

async function pane(name) {
  await driver.findElement(By.css(`.sidebar-item[data-pane="${name}"]`)).click();
}

async function isChecked(id) {
  return driver.executeScript((elementId) => document.getElementById(elementId).checked, id);
}

/** The state the engine reports, read straight from the nftables table in this namespace. */
function firewallTablePresent() {
  try {
    execFileSync("nft", ["list", "table", "inet", "hotspot_guard"], { stdio: "pipe" });
    return true;
  } catch {
    return false;
  }
}

/** Whether `enabled` is set in the named group of the allowlist file the app wrote. */
function groupEnabled(group) {
  const lines = readFileSync(CONFIG, "utf8").split("\n");
  let inside = false;
  for (const line of lines) {
    const trimmed = line.trim();
    if (trimmed.startsWith("[") && trimmed.endsWith("]")) inside = trimmed.slice(1, -1) === group;
    else if (inside && /^enabled\s*=/.test(trimmed)) return /=\s*yes$/.test(trimmed);
  }
  throw new Error(`no enabled line for [${group}] in ${CONFIG}`);
}

before(async () => {
  driver = await new Builder()
    .withCapabilities({
      browserName: "wry",  // the browser name tauri-driver expects for a Tauri app
      "tauri:options": { application: APP },
    })
    .usingServer(DRIVER)
    .build();
  await driver.wait(until.elementLocated(By.id("hero-title")), 60_000, "the app window never appeared");
});

after(async () => {
  if (driver) {
    await driver.quit().catch(() => {});
  }
  // Leave nothing behind: stop any watcher and remove the rules this run may have added.
  try {
    execFileSync("python3", ["-B", enginePath(), "--config", CONFIG, "disable"], { stdio: "pipe" });
  } catch {
    // Nothing was blocking, which is the normal end state.
  }
});

test("the app starts, reads the engine, and writes its allowlist to the app data folder", async () => {
  await waitForText("hero-title", "Blocking is off");
  assert.equal(await isChecked("block-switch"), false);
  assert.ok(existsSync(CONFIG), `the allowlist should be created at ${CONFIG}`);
  assert.match(readFileSync(CONFIG, "utf8"), /^\[social\]$/m);
});

test("Allowed Groups lists the real allowlist groups", async () => {
  await pane("allowed");
  await waitForVisible("pane-allowed");
  const names = await driver.executeScript(() =>
    [...document.querySelectorAll("#groups-list .group-row .row-title")].map((el) => el.textContent));
  assert.deepEqual(names, ["Social", "Claude", "Dev", "Custom"]);
});

test("Data Usage reads the real engine and renders the page", async () => {
  await pane("usage");
  await waitForVisible("pane-usage");
  await driver.wait(async () => (await read("usage-headline-sub")) !== "Loading…", 30_000,
    "the usage page never finished loading");
  const note = (await read("usage-note")).trim();
  assert.ok(!note.startsWith("Usage could not be read"), `the engine failed to report usage: ${note}`);
  assert.equal((await read("usage-headline-label")).trim(), "All traffic");
  await driver.findElement(By.css('.segment[data-view="excluding"]')).click();
  await driver.wait(async () => (await read("usage-headline-label")).trim() === "Excluding social media", 10_000,
    "the excluding view never showed");
  await driver.findElement(By.css('.segment[data-view="all"]')).click();
  await pane("allowed");
});

test("turning a group on is saved to the allowlist file, and off again", async () => {
  assert.equal(groupEnabled("dev"), false);
  await setGroup("dev", true);
  await setGroup("dev", false);
});

test("turning blocking on starts the real watcher, and the firewall rules are in place", async () => {
  await pane("blocking");
  await waitForVisible("pane-blocking");
  await click("#block-switch");
  await waitForText("hero-title", "Blocking is on", 60_000);
  assert.equal(firewallTablePresent(), true, "the hotspot_guard nftables table should exist");
  assert.equal(await isChecked("block-switch"), true);
});

test("the activity log shows what the real watcher did", async () => {
  await pane("activity");
  await driver.wait(async () => (await read("log")).includes("blocking on via nftables"), 30_000,
    "the watcher log never showed the start");
});

test("Preview asks the engine to resolve the allowlist", async () => {
  await click("#preview-btn");
  await waitForVisible("sheet", 60_000);
  assert.match(await read("sheet-summary"), /addresses resolved, \d+ networks would be allowed/);
  await driver.findElement(By.id("sheet-done")).click();
  await waitForHidden("sheet");
});

test("a group changed while blocking shows Changes waiting, and Apply Changes applies it", async () => {
  await pane("allowed");
  await setGroup("custom", true);
  await pane("blocking");
  await waitForVisible("apply-row");
  await click("#apply-btn");
  await waitForHidden("apply-row", 60_000);
  assert.equal(firewallTablePresent(), true, "blocking should still be on after applying");
});

test("turning blocking off removes the real firewall rules", async () => {
  await click("#block-switch");
  await waitForText("hero-title", "Blocking is off", 60_000);
  assert.equal(firewallTablePresent(), false, "the hotspot_guard table should be gone");
});

test("Edit Allowlist reports a failure when no editor can open the file", { skip: XDG_OPEN && "xdg-open is installed here, so the editor would open" }, async () => {
  await driver.findElement(By.id("edit-btn")).click();
  await waitForVisible("alert", 15_000);
  assert.ok((await read("alert-text")).trim().length > 0, "the alert should say why the file did not open");
  await driver.findElement(By.id("alert-ok")).click();
  await waitForHidden("alert");
});
