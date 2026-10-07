# hotspot-guard

Blocks all outbound traffic except an allowlist, so a metered phone hotspot only
carries what you choose. Software updates, cloud sync and background telemetry
fail to connect while blocking is on.

Works on macOS (pf), Linux (nftables) and Windows (Windows Defender Firewall).
Standard library only, Python 3.8 or newer. No install step.

## How it works

- `apply` installs a default-deny **outbound** rule set in the OS firewall. It
  lives in its own pf anchor, nftables table or Windows Firewall group, so
  `disable` removes only what this tool added.
- Always allowed: loopback, DNS (port 53), DHCP, ping, and private/LAN ranges
  (`allow_local_network`, on by default, costs no mobile data).
- Allowed destinations are the IP addresses that the **enabled** groups in
  `hotspot-guard.ini` resolve to. `watch` re-resolves them every
  `refresh_seconds` and remembers recent addresses for `keep_addresses_hours`,
  so a flaky lookup does not cut live connections.
- Inbound traffic is not affected.

## Quick start

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

## Commands

| Command | What it does |
| --- | --- |
| `plan` | Resolves the allowlist and prints each name's addresses and any names with no address. No changes. |
| `apply` | Turns blocking on. `apply --dry-run` prints the firewall rules without installing them. |
| `refresh` | Re-resolves names and updates the live rules once. Fails if blocking is off. |
| `watch` | `apply`, then `refresh` every `refresh_seconds`. Reloads the config file each cycle. |
| `disable` | Removes every hotspot-guard rule and restores normal networking. |
| `status` | Shows whether blocking is on. |

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
   addresses from the last run until `disable`.
7. **Lockout.** If something you need is blocked, run `disable`. Keep a
   terminal open with that command ready before your first `apply`. If
   `watch` crashes, blocking stays on until you run `disable`.

## Where state lives

- macOS: `/var/db/hotspot-guard/`
- Linux: `/var/lib/hotspot-guard/`
- Windows: `%ProgramData%\hotspot-guard\`

Each holds the last rules, the addresses seen, and a `state.json` that
`disable` uses to restore the original Windows Firewall settings.

## Tests

```sh
cd tools/hotspot-guard
python3 -m unittest -v
```

The tests cover config parsing, address collapsing, address memory and rule
rendering. On Linux they also run `nft -c`, which checks the nftables script
without changing any rules.

Status: the Linux path was run end to end in an isolated network namespace
(apply, status, refresh, disable). The macOS (pf) and Windows paths have not yet
been run on real hardware. Run `apply --dry-run` on those systems first and
review the output.

## Not included yet

- Starting `watch` automatically at login. A launchd, systemd or Task Scheduler
  entry would do it.
- A DNS-aware mode that allows by hostname (see limitation 1).
