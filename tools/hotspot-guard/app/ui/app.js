// Hotspot Guard UI. Talks to the Rust shell through Tauri's global API (withGlobalTauri).
const invoke = window.__TAURI__.core.invoke;

const PANE_TITLES = {
  blocking: "Blocking",
  allowed: "Allowed Groups",
  activity: "Activity",
  usage: "Data Usage",
};

const state = {
  status: null,        // last `status --json` result, or null until the first read
  groups: [],          // last `groups --json` result
  pending: false,      // group changes made while blocking that are not applied yet
  busy: false,         // an action is running (possibly waiting for an admin prompt)
  busyMessage: "",     // shown under the switch while busy
  loaded: false,       // true once the first read has succeeded
  switchTarget: null,  // the position the user asked for while an action runs
  usage: { view: "all", hours: 24, data: null, error: null },  // the Data Usage pane
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
  if (state.pane === "usage") await refreshUsage();
}

// ---- Data usage -------------------------------------------------------------------------------

/** 1000-based units, the way network traffic is usually quoted. */
function formatBytes(bytes) {
  if (bytes === null || bytes === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1000 && unit < units.length - 1) {
    value /= 1000;
    unit += 1;
  }
  const shown = unit === 0 || value >= 100 ? Math.round(value) : value.toFixed(1);
  return `${shown} ${units[unit]}`;
}

async function refreshUsage() {
  const usage = state.usage;
  const bucket = usage.hours <= 24 ? 3600 : 86400;
  try {
    usage.data = await invoke("engine_usage", { hours: usage.hours, bucket });
    usage.error = null;
  } catch (error) {
    usage.error = errorText(error);
  }
  renderUsage();
}

function renderUsage() {
  const usage = state.usage;
  document.querySelectorAll(".segment[data-view]").forEach((button) => {
    const selected = button.dataset.view === usage.view;
    button.classList.toggle("is-selected", selected);
    button.setAttribute("aria-checked", String(selected));
  });
  document.querySelectorAll(".segment[data-hours]").forEach((button) => {
    const selected = Number(button.dataset.hours) === usage.hours;
    button.classList.toggle("is-selected", selected);
    button.setAttribute("aria-checked", String(selected));
  });

  const note = $("usage-note");
  if (usage.error) {
    $("usage-headline").textContent = "—";
    note.textContent = `Usage could not be read: ${usage.error}`;
    renderChart([], usage.view);
    return;
  }
  const data = usage.data;
  if (!data) {
    note.textContent = "Loading…";
    return;
  }

  const totals = data.totals;
  const split = totals.social !== null;
  const periodText = usage.hours === 24 ? "in the last 24 hours" : "in the last 7 days";
  const socialShare = split && totals.social + totals.other > 0
    ? Math.round((100 * totals.social) / (totals.social + totals.other))
    : null;

  $("usage-headline-label").textContent = usage.view === "all" ? "All traffic" : "Excluding social media";
  $("usage-headline").textContent = formatBytes(usage.view === "all" ? totals.all : totals.other);
  $("usage-headline-sub").textContent = split || usage.view === "all"
    ? `${periodText}`
    : "Not available on this system";

  $("usage-social").textContent = formatBytes(totals.social);
  $("usage-social-sub").textContent = socialShare === null ? "Not available on this system" : `${socialShare}% of measured traffic`;
  $("usage-other").textContent = formatBytes(totals.other);
  $("usage-other-sub").textContent = socialShare === null ? "Not available on this system" : `${100 - socialShare}% of measured traffic`;

  const notes = [];
  if (data.samples === 0) {
    notes.push("No usage recorded yet. Usage is recorded every minute while blocking is on.");
  } else if (!split) {
    notes.push("Splitting out social media needs per-connection counters, which this system does not provide. Only the totals are shown.");
  } else if (totals.split_coverage !== null && totals.split_coverage < 0.999) {
    notes.push(`The split covers ${Math.round(totals.split_coverage * 100)}% of the measured traffic. The rest had no per-connection counters.`);
  }
  if (!data.tracking) {
    notes.push("Blocking is off, so nothing is being recorded now.");
  }
  note.textContent = notes.join(" ");
  renderChart(data.buckets, usage.view);
}

function renderChart(buckets, view) {
  const svg = $("usage-chart");
  const width = 600;
  const height = 190;
  if (!buckets.length) {
    svg.innerHTML = "";
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    $("axis-start").textContent = "";
    $("axis-end").textContent = "";
    return;
  }
  const value = (bucket) => (view === "all" ? bucket.total : bucket.other);
  const max = Math.max(1, ...buckets.map(value));
  const slot = width / buckets.length;
  const barWidth = Math.max(2, slot * 0.7);
  let markup = "";
  buckets.forEach((bucket, index) => {
    const x = (index * slot + (slot - barWidth) / 2).toFixed(1);
    let top = height;
    const segment = (amount, className) => {
      if (!amount) return;
      const h = (amount / max) * (height - 4);
      top -= h;
      markup += `<rect x="${x}" y="${top.toFixed(1)}" width="${barWidth.toFixed(1)}" height="${h.toFixed(1)}" class="${className}"/>`;
    };
    if (view === "all") {
      segment(bucket.other, "bar-other");
      segment(bucket.social, "bar-social");
      segment(bucket.total - bucket.split, "bar-unsplit");
    } else {
      segment(bucket.other, "bar-other");
    }
  });
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.innerHTML = markup;
  const label = (start) => new Date(start * 1000).toLocaleString([], {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
  $("axis-start").textContent = label(buckets[0].start);
  $("axis-end").textContent = label(buckets[buckets.length - 1].start);
}

document.querySelector("#pane-usage").addEventListener("click", (event) => {
  const viewButton = event.target.closest(".segment[data-view]");
  const hoursButton = event.target.closest(".segment[data-hours]");
  if (viewButton) {
    state.usage.view = viewButton.dataset.view;
    renderUsage();
  } else if (hoursButton) {
    state.usage.hours = Number(hoursButton.dataset.hours);
    state.usage.data = null;
    renderUsage();
    refreshUsage();
  }
});

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
  if (pane === "usage") refreshUsage();
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
