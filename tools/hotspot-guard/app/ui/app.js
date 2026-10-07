// Hotspot Guard UI. Talks to the Rust shell through Tauri's global API (withGlobalTauri).
const invoke = window.__TAURI__.core.invoke;

const PANE_TITLES = {
  blocking: "Blocking",
  allowed: "Allowed Groups",
  activity: "Activity",
};

const state = {
  status: null,        // last `status --json` result, or null until the first read
  groups: [],          // last `groups --json` result
  pending: false,      // group changes made while blocking that are not applied yet
  busy: false,         // an action is running (possibly waiting for an admin prompt)
  busyMessage: "",     // shown under the switch while busy
  loaded: false,       // true once the first read has succeeded
  switchTarget: null,  // the position the user asked for while an action runs
  pane: "blocking",
};

const $ = (id) => document.getElementById(id);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function esc(text) {
  return String(text).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function titleCase(text) {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

// "unknown" until the first read, then "off", "on", or "stale" (rules recorded, watcher gone).
function blockState(status) {
  if (!status) return "unknown";
  if (!status.active) return "off";
  return status.watching ? "on" : "stale";
}

// Rust errors arrive as plain strings. Errors raised in this file are Error objects.
function errorText(error) {
  return error instanceof Error ? error.message : String(error);
}

// Cancelling the administrator prompt is not an error worth an alert.
function isQuietCancel(error) {
  return errorText(error).startsWith("Administrator permission");
}

function showBanner(text) {
  const banner = $("banner");
  banner.textContent = text;
  banner.hidden = !text;
}

function showAlert(text) {
  $("alert-text").textContent = text;
  $("alert").hidden = false;
  $("alert-ok").focus();
}

// ---- Rendering --------------------------------------------------------------------------------

function render() {
  const bs = blockState(state.status);
  const on = bs === "on";
  const status = state.status || {};

  const hero = $("hero");
  hero.classList.toggle("is-on", on);
  hero.classList.toggle("is-stale", bs === "stale");
  $("hero-title").textContent = {
    unknown: "Checking…",
    off: "Blocking is off",
    on: "Blocking is on",
    stale: "Rules may still be active",
  }[bs];
  $("hero-detail").textContent = {
    unknown: "",
    off: "Everything can reach the internet. Turn blocking on to allow only the enabled groups.",
    on: "Only the enabled groups can connect. Their addresses refresh automatically.",
    stale: "The watcher is not running, so the rules are no longer maintained.",
  }[bs];

  const toggle = $("block-switch");
  toggle.checked = state.busy && state.switchTarget !== null ? state.switchTarget : on;
  toggle.disabled = state.busy || bs === "unknown";
  $("switch-sub").textContent = state.busyMessage || (on
    ? "Only enabled groups can connect"
    : "Turn on to block everything except the enabled groups");

  $("apply-row").hidden = !(on && state.pending);
  $("apply-btn").disabled = state.busy;
  $("stale-row").hidden = bs !== "stale";
  $("clear-btn").disabled = state.busy;
  $("preview-btn").disabled = state.busy;

  const sidebarText = { unknown: "Checking…", off: "Blocking off", on: "Blocking on", stale: "Rules still active" }[bs];
  $("sidebar-status").textContent = sidebarText;
  $("sidebar-dot").className = `status-dot${on ? " is-on" : ""}${bs === "stale" ? " is-stale" : ""}`;

  $("watcher-value").textContent = status.watching ? `Running (pid ${status.watcher_pid})` : "Not running";
  $("nat64-value").textContent = status.nat64_prefix || (status.active ? "Not used" : "—");
  $("addresses-value").textContent = status.remembered != null ? String(status.remembered) : "—";
  $("applied-value").textContent = status.applied_at
    ? new Date(status.applied_at * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })
    : "Never";

  renderGroups();
}

function renderGroups() {
  if (!state.loaded) {
    $("groups-list").innerHTML = '<div class="row placeholder"><div class="row-title muted">Loading…</div></div>';
    return;
  }
  if (state.groups.length === 0) {
    $("groups-list").innerHTML = `<div class="row placeholder"><div class="row-text">
      <div class="row-title">No groups yet</div>
      <div class="row-sub">Add domains or IP ranges to a group with Edit Allowlist.</div></div></div>`;
    return;
  }
  $("groups-list").innerHTML = state.groups.map((group) => {
    const name = titleCase(group.name);
    const count = group.domain_count === 1 ? "1 entry" : `${group.domain_count} entries`;
    return `
      <div class="row group-row">
        <div class="row-text">
          <div class="row-title">${esc(name)}</div>
          <div class="row-sub">${esc(group.description)}</div>
        </div>
        <span class="group-count">${esc(count)}</span>
        <label class="switch">
          <input type="checkbox" data-group="${esc(group.name)}" aria-label="${esc(name)}"
                 ${group.enabled ? "checked" : ""} ${state.busy ? "disabled" : ""}>
          <span class="track"></span>
          <span class="thumb"></span>
        </label>
      </div>`;
  }).join("");
}

function fillSheet(plan) {
  const addresses = plan.addresses || [];
  $("sheet-summary").textContent =
    `${addresses.length} addresses resolved, ${plan.networks} networks would be allowed.`;
  $("plan-list").innerHTML = addresses.length
    ? addresses.map((row) => `<li><span>${esc(row.host)}</span><span>${esc(row.ip)}</span></li>`).join("")
    : "<li><span>No addresses. Turn a group on first.</span></li>";
  const failed = plan.failed || [];
  $("plan-failed").hidden = failed.length === 0;
  $("plan-failed").textContent = failed.length
    ? `No address for ${failed.join(", ")}. ${failed.length === 1 ? "It stays" : "These stay"} blocked.`
    : "";
}

// ---- Reads ------------------------------------------------------------------------------------

async function refresh() {
  try {
    const [status, groups] = await Promise.all([invoke("engine_status"), invoke("engine_groups")]);
    state.status = status;
    state.groups = groups;
    state.loaded = true;
    if (blockState(status) !== "on") state.pending = false;
    showBanner("");
  } catch (error) {
    showBanner(String(error));
    return;
  }
  render();
  if (state.pane === "activity") await refreshLog();
}

async function refreshLog() {
  const pre = $("log");
  try {
    const lines = await invoke("read_log", { lines: 300 });
    const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
    pre.textContent = lines.length ? lines.join("\n") : "No activity yet. Turn blocking on to start the watcher.";
    if (atBottom) pre.scrollTop = pre.scrollHeight;
  } catch (error) {
    pre.textContent = String(error);
  }
}

// ---- Actions ----------------------------------------------------------------------------------

function setBusy(busy, message = "") {
  state.busy = busy;
  state.busyMessage = busy ? message : "";
  render();
}

/// Runs a command that may ask for administrator rights. The controls stay busy until it finishes.
/// `settle` is an optional async check that holds the busy state until the change is visible.
async function runAction(command, args, message, { settle = null, onSuccess = null } = {}) {
  setBusy(true, message);
  try {
    const result = await invoke(command, args);
    if (settle) await settle();
    if (onSuccess) onSuccess(result);
    return result;
  } catch (error) {
    if (!isQuietCancel(error)) showAlert(errorText(error));
    return undefined;
  } finally {
    state.switchTarget = null;
    setBusy(false);
    await refresh();
  }
}

/// Waits (up to about 10 seconds) until the watcher reports it is running. If it never does,
/// the watcher has failed, so the reason is in its log: say so instead of leaving the switch to snap back.
async function waitForWatcher() {
  setBusy(true, "Starting the watcher…");
  for (let attempt = 0; attempt < 20; attempt += 1) {
    const status = await invoke("engine_status");
    if (status.watching) return;
    await sleep(500);
  }
  const lines = await invoke("read_log", { lines: 6 });
  throw new Error(`The watcher did not start.\n\n${lines.join("\n") || "No log was written."}`);
}

$("block-switch").addEventListener("change", async (event) => {
  const turnOn = event.target.checked;
  state.switchTarget = turnOn;
  if (turnOn) {
    await runAction("start_blocking", {}, "Waiting for permission to start blocking…", {
      settle: waitForWatcher,
    });
  } else {
    await runAction("stop_blocking", {}, "Waiting for permission to turn blocking off…");
  }
});

$("apply-btn").addEventListener("click", () => {
  runAction("apply_changes", {}, "Waiting for permission to apply changes…", {
    onSuccess: () => { state.pending = false; },
  });
});

$("clear-btn").addEventListener("click", () => {
  state.switchTarget = false;
  runAction("stop_blocking", {}, "Waiting for permission to clear the rules…");
});

$("groups-list").addEventListener("change", async (event) => {
  const input = event.target;
  if (!(input instanceof HTMLInputElement) || !input.dataset.group) return;
  const enabled = input.checked;
  const wasOn = blockState(state.status) === "on";
  setBusy(true, "Saving…");
  try {
    await invoke("set_group", { name: input.dataset.group, enabled });
    if (wasOn) state.pending = true;
  } catch (error) {
    showAlert(errorText(error));
  } finally {
    setBusy(false);
    await refresh();
  }
});

$("preview-btn").addEventListener("click", async () => {
  setBusy(true, "Resolving names…");
  try {
    fillSheet(await invoke("engine_plan"));
    $("sheet").hidden = false;
    $("sheet-done").focus();
  } catch (error) {
    showAlert(errorText(error));
  } finally {
    setBusy(false);
  }
});

$("edit-btn").addEventListener("click", async () => {
  try {
    await invoke("open_allowlist");
  } catch (error) {
    showAlert(errorText(error));
  }
});

$("sheet-done").addEventListener("click", () => { $("sheet").hidden = true; });
$("alert-ok").addEventListener("click", () => { $("alert").hidden = true; });

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    $("sheet").hidden = true;
    $("alert").hidden = true;
  }
});

// ---- Navigation -------------------------------------------------------------------------------

function selectPane(pane) {
  state.pane = pane;
  document.querySelectorAll(".sidebar-item").forEach((item) => {
    const selected = item.dataset.pane === pane;
    item.classList.toggle("is-selected", selected);
    if (selected) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
  document.querySelectorAll(".pane").forEach((section) => {
    section.hidden = section.id !== `pane-${pane}`;
  });
  $("pane-title").textContent = PANE_TITLES[pane];
  if (pane === "activity") refreshLog();
}

document.querySelector(".sidebar-list").addEventListener("click", (event) => {
  const item = event.target.closest(".sidebar-item");
  if (item) selectPane(item.dataset.pane);
});

// ---- Start ------------------------------------------------------------------------------------

async function init() {
  try {
    document.documentElement.dataset.platform = await invoke("platform");
  } catch (_) {
    // Opened outside the Tauri shell (for example in a browser for design review). Keep the default look.
  }
  selectPane("blocking");
  render();
  await refresh();
  setInterval(refresh, 4000);
}

init();
