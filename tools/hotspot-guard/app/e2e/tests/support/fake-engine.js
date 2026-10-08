// A stand-in for the Tauri bridge (window.__TAURI__). It keeps a little engine state, so the
// interface behaves like the real one: starting blocking brings the watcher up, turning it off
// clears it, and group changes stick. Failures are injected through the options.
const { expect } = require("@playwright/test");

const DEFAULT_GROUPS = [
  { name: "social", description: "Facebook, Instagram, WhatsApp, X, TikTok, Snapchat, Reddit, LinkedIn and more", enabled: true, domain_count: 38 },
  { name: "claude", description: "Claude Code: API, login and feature flags", enabled: true, domain_count: 7 },
  { name: "dev", description: "GitHub and npm: pull, push and package installs", enabled: false, domain_count: 6 },
  { name: "custom", description: "Your own domains, IP addresses or CIDR ranges", enabled: false, domain_count: 0 },
];

const DEFAULT_PLAN = {
  addresses: [
    { host: "api.anthropic.com", ip: "160.79.104.10" },
    { host: "bsky.app", ip: "3.12.80.176" },
    { host: "facebook.com", ip: "157.240.254.35" },
  ],
  failed: ["twimg.com"],
  networks: 83,
};

function defaults(overrides) {
  return {
    platform: "linux",
    status: { active: false, watching: false, watcher_pid: null, applied_at: null, remembered: 0, backend: "nftables", log_file: "/var/lib/hotspot-guard/watch.log", nat64_prefix: null },
    groups: DEFAULT_GROUPS,
    plan: DEFAULT_PLAN,
    log: [],
    startDelayMs: 250,
    startFailure: null,   // the watcher starts, then fails with this log line and never reports watching
    startCancel: false,   // the administrator prompt is dismissed
    stopCancel: false,
    applyCancel: false,
    groupFailure: null,
    statusFailure: null,  // engine_status throws this message
    openFailure: null,
    planFailure: null,
    usageMode: "split",   // "split", "unsplit" (no per-connection counters), or "empty"
    usageFailure: null,
    tracking: true,
    ...overrides,
  };
}

/// The usage the engine would report for a period, with one bucket per hour or day.
function usageFixture({ hours, bucket, mode, tracking }) {
  const count = Math.round((hours * 3600) / bucket);
  const now = Math.floor(Date.now() / 1000 / bucket) * bucket;
  const buckets = [];
  for (let i = 0; i < count && mode !== "empty"; i += 1) {
    const other = 200000 + (i % 5) * 50000;
    const social = 300000 + (i % 3) * 100000;
    const start = now - (count - 1 - i) * bucket;
    if (mode === "split") buckets.push({ start, total: other + social, social, other, split: other + social });
    else buckets.push({ start, total: other + social, social: 0, other: 0, split: 0 });
  }
  const sum = (key) => buckets.reduce((acc, b) => acc + b[key], 0);
  const all = sum("total");
  const split = sum("split");
  return {
    hours,
    bucket_seconds: bucket,
    samples: mode === "empty" ? 0 : count,
    last_sample: mode === "empty" ? null : now,
    methods: mode === "split" ? ["conntrack"] : [],
    tracking,
    totals: {
      all,
      social: mode === "split" ? sum("social") : null,
      other: mode === "split" ? sum("other") : null,
      split_coverage: all ? split / all : null,
    },
    buckets,
  };
}

/// Installs the fake bridge before the page loads. `overrides` sets the starting situation.
async function installFakeEngine(page, overrides = {}) {
  const options = defaults(overrides);
  await page.addInitScript((opts) => {
    // The init script runs in the page, so the usage helper travels as source text.
    const usageFixture = eval("(" + opts.fixtureSource + ")");
    const state = {
      options: { ...opts },
      status: { ...opts.status },
      groups: opts.groups.map((group) => ({ ...group })),
      log: [...opts.log],
      calls: [],
    };
    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    const DENIED = "Administrator permission was not granted.";
    const handlers = {
      platform: () => state.options.platform,
      engine_status: async () => {
        if (state.options.statusFailure) throw state.options.statusFailure;
        return { ...state.status };
      },
      engine_groups: async () => state.groups.map((group) => ({ ...group })),
      engine_plan: async () => {
        if (state.options.planFailure) throw state.options.planFailure;
        return { ...state.options.plan };
      },
      read_log: async ({ lines }) => state.log.slice(-lines),
      set_group: async ({ name, enabled }) => {
        if (state.options.groupFailure) throw state.options.groupFailure;
        const group = state.groups.find((item) => item.name === name);
        if (!group) throw `no such group: ${name}`;
        group.enabled = enabled;
        return null;
      },
      // An administrator prompt waits for the user, so a cancel arrives after a delay, not at once.
      start_blocking: async () => {
        await sleep(state.options.startDelayMs);
        if (state.options.startCancel) throw DENIED;
        if (state.options.startFailure) {
          state.log.push(state.options.startFailure);
          return null;
        }
        state.status = { ...state.status, active: true, watching: true, watcher_pid: 4242, applied_at: Date.now() / 1000, remembered: 78 };
        state.log.push("blocking on via nftables: 83 networks allowed");
        return null;
      },
      stop_blocking: async () => {
        await sleep(state.options.startDelayMs);
        if (state.options.stopCancel) throw DENIED;
        state.status = { ...state.status, active: false, watching: false, watcher_pid: null, applied_at: null, remembered: 0 };
        return "blocking off; normal networking restored";
      },
      apply_changes: async () => {
        await sleep(state.options.startDelayMs);
        if (state.options.applyCancel) throw DENIED;
        return "refreshed";
      },
      engine_usage: async ({ hours, bucket }) => {
        if (state.options.usageFailure) throw state.options.usageFailure;
        return usageFixture({ hours, bucket, mode: state.options.usageMode, tracking: state.options.tracking });
      },
      open_allowlist: async () => {
        if (state.options.openFailure) throw state.options.openFailure;
        return null;
      },
    };
    window.__fake = {
      calls: state.calls,
      /** The watcher dies without a stop, as after a crash. The rules stay recorded as active. */
      kill() {
        state.status = { ...state.status, watching: false, watcher_pid: null };
      },
      /** Change how the engine behaves from now on. */
      set(partial) {
        Object.assign(state.options, partial);
      },
    };
    window.__TAURI__ = {
      core: {
        invoke: async (command, args) => {
          state.calls.push({ command, args: args ?? null });
          const handler = handlers[command];
          if (!handler) throw `unknown command: ${command}`;
          return handler(args ?? {});
        },
      },
    };
  }, { ...options, fixtureSource: usageFixture.toString() });
}

/// Opens the interface and waits until the first status read has been shown.
async function openApp(page, overrides = {}) {
  await installFakeEngine(page, overrides);
  await page.goto("/index.html");
  await expect(page.locator("#hero-title")).not.toHaveText("Checking…");
}

/// The bridge calls the fake engine recorded, in order.
async function commands(page) {
  return page.evaluate(() => window.__fake.calls.map((call) => call.command));
}

module.exports = { installFakeEngine, openApp, commands, DEFAULT_GROUPS, DEFAULT_PLAN };
