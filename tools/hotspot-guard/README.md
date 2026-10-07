# hotspot-guard

Blocks all outbound traffic except an allowlist, so a metered phone hotspot only
carries what you choose. Software updates, cloud sync and background telemetry
fail to connect while blocking is on.

Works on macOS (pf), Linux (nftables) and Windows (Windows Defender Firewall),
from a desktop app or the command line. The engine is standard-library Python,
3.8 or newer, and there is no install step for it.

## How it works

- Blocking installs a default-deny **outbound** rule set in the OS firewall. The
  rules live in their own pf anchor, nftables table or Windows Firewall group, so
  turning blocking off removes only what this tool added.
- Always allowed: loopback, DNS (port 53), DHCP, ping, and private/LAN ranges
  (`allow_local_network`, on by default, costs no mobile data).
- Allowed destinations are the IP addresses that the **enabled** groups in
  `hotspot-guard.ini` resolve to. A watcher re-resolves them every
  `refresh_seconds` and remembers recent addresses for `keep_addresses_hours`,
  so a flaky lookup does not cut live connections.
- Inbound traffic is not affected.

## Desktop app

`app/` is a Tauri app for Windows, macOS and Linux. Its interface is styled after
macOS: a sidebar with a translucent material on macOS, grouped inset lists,
switches, sheets and alerts, in light and dark appearance. It is a web
imitation, not native AppKit controls, so it will not look identical to a
System Settings pane.

Sections:
- **Blocking**: the main switch, the watcher state, and the Apply Changes or Clear Rules
  actions when they apply.
- **Allowed Groups**: the groups, with a switch for each.
- **Activity**: the watcher log.
- **Preview** (toolbar): the names resolved right now, with any name that has no
  address called out.
- **Edit Allowlist** (toolbar): opens the config in your text editor.

Turning blocking on or off, and applying group changes while blocking is on,
needs administrator rights. The app asks the OS for them each time:
- macOS: the standard password dialog.
- Linux: the polkit dialog (`pkexec`).
- Windows: the UAC prompt.

The app itself never runs with elevated rights. Reads, group toggles and the
preview do not prompt.

### Build

You need Python 3.8 or newer, a stable Rust toolchain, and the Tauri prerequisites
for your OS (see https://tauri.app/start/prerequisites/). On Linux that means the
WebKitGTK 4.1 development packages. On Windows it means the MSVC build tools and
WebView2, which Windows 10 and 11 already include.

```sh
cargo install tauri-cli --version "^2" --locked   # once
cd tools/hotspot-guard/app/src-tauri
cargo tauri build                                 # bundles for this OS in target/release/bundle
```

For development, run `cargo tauri dev` from `app/src-tauri`. The app then runs
the engine straight from the source tree.

The icons in `src-tauri/icons/` are placeholders from `app/make-icons.py`. To use real
artwork, put a 1024 px PNG through `cargo tauri icon <file>`.

### Where things live

| | macOS | Linux | Windows |
| --- | --- | --- | --- |
| Allowlist (you edit this) | `~/Library/Application Support/local.hotspot-guard/hotspot-guard.ini` | `~/.local/share/local.hotspot-guard/hotspot-guard.ini` | `%APPDATA%\local.hotspot-guard\hotspot-guard.ini` |
| Rules and state | `/var/db/hotspot-guard/` | `/var/lib/hotspot-guard/` | `%ProgramData%\hotspot-guard\` |
| Watcher log | `/var/db/hotspot-guard/watch.log` | `/var/lib/hotspot-guard/watch.log` | `%ProgramData%\hotspot-guard\watch.log` |

The app copies the default allowlist into the first location the first time it runs.

### Status

- **Linux**: `cargo tauri build --debug --bundles deb` produces a `.deb` that
  contains the engine. The window was checked under a virtual display: the
  states, the preview sheet, the alerts and the watcher-failure path. The
  elevation path has only been checked for its failure case, with `pkexec` absent.
- **Windows**: the Rust code type-checks for the Windows target. It has not been run.
- **macOS**: not type-checked, because its Objective-C dependencies need the Apple
  SDK. Not run either.

Try `cargo tauri build` on macOS and Windows, and report any problems you hit.

## Command line

Everything except `plan` changes or reads firewall state, so it needs
administrator rights.

**macOS** (Terminal)
```sh
cd tools/hotspot-guard
python3 hotspot_guard.py plan             # what would be allowed; changes nothing
sudo python3 hotspot_guard.py watch       # block everything else; keep this window open
```

**Linux** (needs `nftables`, e.g. `sudo apt install nftables`)
```sh
cd tools/hotspot-guard
python3 hotspot_guard.py plan
sudo python3 hotspot_guard.py watch
```

**Windows** (PowerShell opened as Administrator)
```powershell
cd tools\hotspot-guard
py -3 hotspot_guard.py plan
py -3 hotspot_guard.py watch
```

Stop refreshing with Ctrl-C. Blocking stays on until you run:
```sh
sudo python3 hotspot_guard.py disable     # Windows: run from elevated PowerShell
```

### Commands

| Command | What it does |
| --- | --- |
| `plan` | Resolves the allowlist and prints each name's addresses and any names with no address. No changes. `plan --json` gives the same as JSON. |
| `apply` | Turns blocking on. `apply --dry-run` prints the firewall rules without installing them. |
| `refresh` | Re-resolves names and updates the live rules once. Fails if blocking is off. |
| `watch` | `apply`, then `refresh` every `refresh_seconds`. Reloads the config file each cycle. Stops when `disable` writes its stop file, or on Ctrl-C. |
| `disable` | Stops the watcher, then removes every hotspot-guard rule and restores normal networking. |
| `status` | Shows whether blocking is on. With sudo it asks the firewall itself. `status --json` reads only what the tool recorded, so it needs no sudo. |
| `groups` | Lists allowlist groups, whether each is on, and how many entries it has. `--json` for scripts. |
| `set-group NAME on\|off` | Turns a group on or off in the config. Only that group's `enabled` line changes. Run `refresh` to apply it. |

Add `--config path/to/file.ini` to any command to use another allowlist.

## The allowlist (`hotspot-guard.ini`)

Each section other than `[settings]` is a group. Set `enabled = yes` to allow it.

- `[social]`: Facebook, Instagram, WhatsApp, X, TikTok, Snapchat, Reddit,
  LinkedIn, Pinterest, Bluesky, Discord. On by default.
- `[claude]`: Claude Code's API, login and feature flags. On by default.
  Claude Code's own update downloads are not listed, so they stay blocked.
- `[dev]`: GitHub and npm. Off by default, so turn it on when you need to pull or push.
- `[custom]`: your own domains, IP addresses or CIDR ranges.

Edit the file, then run `refresh` for an immediate effect. A running `watch`
picks up changes at its next cycle. A name matches only itself. Subdomains
must be listed separately, for example `www.facebook.com` and `m.facebook.com`.

To add an app, run `plan` and watch the output. Add any hostname the app needs
to its group, then `refresh`.

## Check it works

```sh
# Should connect (any HTTP status is fine; it proves the TCP and TLS path works):
curl -sS --max-time 8 -o /dev/null -w "%{http_code}\n" https://api.anthropic.com
# Should time out while blocking is on:
curl -sS --max-time 8 -o /dev/null https://example.com
```
On Windows use `curl.exe`. Run `python3 hotspot_guard.py status` to confirm the state.

## Limitations: read before relying on it

1. **It filters by IP address, not by hostname.** Names are resolved on your
   machine and their addresses are allowed. Anything not listed is blocked,
   including subdomains. Large CDN hostnames often have no address at the
   apex: `plan` reports them as `no address`. Social-media photos and video
   served from those CDNs may break until you list the specific hostnames
   your apps use. This is the main gap, and a DNS-aware resolver would close it.
2. **Shared IPs leak.** Cloudflare, Fastly, Akamai and Google serve many sites
   from the same addresses, so allowing one site can also allow others on the
   same IP.
3. **No per-app rules** on macOS or Linux. Claude Code is allowed by its
   domains, so any other program talking to those domains gets through too.
   Windows could add per-program rules; not implemented.
4. **Blocked by design:** iCloud, Apple push notifications, Windows Update,
   OneDrive, Google services and app-store traffic. Push notifications for
   apps that are not allowed will not arrive.
5. **QUIC (HTTP/3, UDP 443) is blocked.** Browsers and apps fall back to TCP.
6. **Reboots.** macOS and Linux rules are not persistent, so a reboot turns
   blocking off. On Windows the rules persist and blocking stays on with the
   addresses from the last run until disabled. The app then shows **Rules may
   still be active**, and **Clear Rules** removes them.
7. **Lockout.** If something you need is blocked, turn blocking off. Keep the
   app or a terminal ready to do that before your first time turning it on. If
   the watcher crashes, blocking stays on until you turn it off.
8. **IPv6-only networks.** On a NAT64 network the engine finds the translation
   prefix automatically (RFC 7050) and allows IPv4 destinations under it. Some
   carriers do not answer that check, so the prefix may show as `Not used` when it is
   needed. Set `nat64_prefix` to the carrier's `/96` in the allowlist if so.
   [ON-DEVICE-TEST.md](ON-DEVICE-TEST.md) step 1 shows how to tell.
9. **Python is a dependency** of the app. Windows machines usually need it installed
   from python.org. Bundling Python, or porting the engine to Rust, would remove that.

## Tests

`./test-all.sh` runs every tier and says why any tier it cannot run here was skipped.
Run it with `sudo` to include the tiers that need root.

| Tier | What it proves | Needs |
| --- | --- | --- |
| Engine unit tests (`python3 -m unittest`) | Config parsing, address handling, NAT64 and DHCPv6 rules, rendering, group editing, JSON output, the watcher's stale-PID handling | Python 3.8+ |
| Namespace firewall tests (`sudo python3 -m unittest discover -s e2e -v`) | Real packets through nftables between a client and a simulated hotspot: drop and allow for IPv4, the local network, refresh, watch and disable, and that rules are removed. IPv6 and NAT64 cases skip on kernels without IPv6. | root, `ip`, `nft` |
| Browser interface tests (`cd app/e2e && npm install && npx playwright test`) | 39 flows in Chromium, covering every control, dialog, failure path and keyboard path, against a stateful stand-in for the app's bridge | Node 18+, Chromium |
| Real-app tests (`cd app/e2e-app && npm install && sudo ./run-tests.sh`) | The built app, driven through tauri-driver, checking the allowlist file, the firewall table and the watcher log. Runs in its own network namespace. | root, a debug build, `tauri-driver`, `WebKitWebDriver`, polkit, an X display |

What no test can prove is how your network and carrier behave. For that, follow
[ON-DEVICE-TEST.md](ON-DEVICE-TEST.md) on the actual VOXI hotspot, and send back its results.

## Not included yet

- Starting `watch` automatically at login. A launchd, systemd or Task Scheduler
  entry would do it.
- A DNS-aware mode that allows by hostname (see limitation 1).
- Code signing and notarisation for macOS and Windows, so the apps open without warnings.
