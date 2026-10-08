#!/usr/bin/env python3
"""hotspot-guard: allow only listed destinations while on a metered connection.

Installs default-deny outbound rules with the OS firewall: pf on macOS,
nftables on Linux, Windows Defender Firewall on Windows. The allowed
destinations are the addresses the allowlist names resolve to, refreshed
while `watch` runs. Standard library only, Python 3.8 or newer.

  plan       resolve the allowlist and print it (changes nothing)
  apply      turn blocking on (root or administrator)
  refresh    re-resolve the allowlist and update the live rules once
  watch      apply, then refresh every refresh_seconds until stopped
  disable    stop the watcher and remove every hotspot-guard rule
  status     report whether blocking is on
  groups     list allowlist groups
  set-group  turn an allowlist group on or off in the config file
"""

import argparse
import configparser
import ipaddress
import json
import os
import platform
import re
import signal
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import firewall
import usage

SYSTEM = platform.system()
HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "hotspot-guard.ini"
# HOTSPOT_GUARD_STATE_DIR moves the state folder, which the tests use so they never touch real history.
STATE_DIR = Path(os.environ["HOTSPOT_GUARD_STATE_DIR"]) if os.environ.get("HOTSPOT_GUARD_STATE_DIR") else {
    "Darwin": Path("/var/db/hotspot-guard"),
    "Linux": Path("/var/lib/hotspot-guard"),
    "Windows": Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "hotspot-guard",
}.get(SYSTEM, Path.home() / ".hotspot-guard")
STATE_FILE = STATE_DIR / "state.json"
PID_FILE = STATE_DIR / "watch.pid"
STOP_FILE = STATE_DIR / "watch.stop"  # `disable` writes this; the watch loop exits when it sees it
LOG_FILE = STATE_DIR / "watch.log"

LOOPBACK = ["127.0.0.0/8", "::1/128"]
LOCAL_NETWORKS = [
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",  # private IPv4, incl. the hotspot subnet
    "224.0.0.0/4", "fc00::/7", "fe80::/10", "ff00::/8",                 # multicast and IPv6 link-local or ULA
]
HOSTNAME_RE = re.compile(r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def read_parser(path):
    parser = configparser.ConfigParser(interpolation=None)
    if not parser.read(path, encoding="utf-8"):
        sys.exit(f"config file not found: {path}")
    return parser


def load_config(path):
    """Return (settings, networks, names) for the enabled groups in the config file."""
    parser = read_parser(path)
    settings = {
        "allow_local_network": parser.getboolean("settings", "allow_local_network", fallback=True),
        "refresh_seconds": max(60, parser.getint("settings", "refresh_seconds", fallback=300)),
        "keep_addresses_hours": parser.getfloat("settings", "keep_addresses_hours", fallback=6.0),
        "nat64_prefix": parser.get("settings", "nat64_prefix", fallback="auto").strip().lower(),
    }
    networks, names = set(), set()
    for section in parser.sections():
        if section == "settings" or not parser.getboolean(section, "enabled", fallback=False):
            continue
        section_networks, section_names = parse_entries(section, parser.get(section, "domains", fallback=""))
        networks |= section_networks
        names |= section_names
    return settings, networks, names


def parse_entries(section, text):
    """(networks, names) for the entries of one group: IP addresses and CIDR ranges, or domain names."""
    networks, names = set(), set()
    for entry in text.split():
        entry = entry.lower().rstrip(".")
        try:
            networks.add(str(ipaddress.ip_network(entry, strict=False)))
        except ValueError:
            if not HOSTNAME_RE.match(entry):
                sys.exit(f"[{section}] not a domain, IP address or CIDR range: {entry}")
            names.add(entry)
    return networks, names


def social_members(path, group="social"):
    """(networks, names) of the social group, or empty sets when it is off. Used to split usage."""
    parser = read_parser(path)
    if not parser.has_section(group) or not parser.getboolean(group, "enabled", fallback=False):
        return set(), set()
    return parse_entries(group, parser.get(group, "domains", fallback=""))


def list_groups(path):
    parser = read_parser(path)
    return [
        {
            "name": section,
            "description": parser.get(section, "description", fallback=""),
            "enabled": parser.getboolean(section, "enabled", fallback=False),
            "domain_count": len(parser.get(section, "domains", fallback="").split()),
        }
        for section in parser.sections() if section != "settings"
    ]


def set_group(path, name, enabled):
    """Flip one group's enabled line. Every other line, including comments, is kept as written."""
    parser = read_parser(path)
    if name == "settings" or not parser.has_section(name):
        sys.exit(f"no such group: {name}")
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    current, done = None, False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1].strip()
        elif current == name and re.match(r"enabled\s*=", stripped):
            lines[i] = f"enabled = {'yes' if enabled else 'no'}\n"
            done = True
            break
    if not done:
        sys.exit(f"[{name}] has no enabled line")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("".join(lines), encoding="utf-8")
    tmp.replace(path)


def lookup(host):
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return []
    return [info[4][0].split("%")[0] for info in infos]


def resolve(names):
    """Return ({ip: host}, [hosts with no address]) for the given names."""
    hosts = sorted(names)
    found, failed = {}, []
    with ThreadPoolExecutor(max_workers=16) as pool:
        for host, ips in zip(hosts, pool.map(lookup, hosts)):
            if not ips:
                failed.append(host)
            for ip in ips:
                found.setdefault(ip, host)
    return found, failed


def remember(state, fresh, keep_hours):
    """Union of this lookup and recently seen addresses, so a failed lookup does not drop live connections."""
    return remember_field(state, "seen", fresh, keep_hours)


def remember_field(state, field, fresh, keep_hours):
    now = time.time()
    seen = {ip: seen_at for ip, seen_at in state.get(field, {}).items() if now - seen_at < keep_hours * 3600}
    seen.update(dict.fromkeys(fresh, now))
    state[field] = seen
    return list(seen)


def nat64_prefix_for(setting):
    """The NAT64 /96 prefix that IPv4 destinations are reached through, or None.

    'off' turns the feature off. 'auto' asks the network (RFC 7050). Anything else must be
    an IPv6 /96 prefix, for carriers where the auto check does not work.
    """
    if setting == "off":
        return None
    if setting == "auto":
        return discover_nat64_prefix()
    try:
        prefix = ipaddress.IPv6Network(setting, strict=False)
    except ValueError:
        sys.exit(f"[settings] nat64_prefix must be auto, off or an IPv6 /96 prefix: {setting}")
    if prefix.prefixlen != 96:
        sys.exit(f"[settings] nat64_prefix must be a /96 prefix: {setting}")
    return prefix


def discover_nat64_prefix():
    """RFC 7050: ipv4only.arpa only has IPv6 addresses on a NAT64 network, and they embed
    the well-known IPv4 addresses 192.0.0.170 or 192.0.0.171. The prefix is what is left
    once those last 32 bits are cleared."""
    try:
        infos = socket.getaddrinfo("ipv4only.arpa", None, socket.AF_INET6)
    except OSError:
        return None
    for info in infos:
        value = int(ipaddress.IPv6Address(info[4][0].split("%")[0]))
        if str(ipaddress.IPv4Address(value & 0xFFFFFFFF)) in ("192.0.0.170", "192.0.0.171"):
            return ipaddress.IPv6Network((value & ~0xFFFFFFFF, 96))
    return None


def synthesize_nat64(networks, prefix):
    """IPv6 networks that reach each IPv4 network through the NAT64 gateway (RFC 6052, /96)."""
    out = []
    for net in networks:
        parsed = ipaddress.ip_network(net, strict=False)
        if parsed.version == 4:
            value = int(prefix.network_address) | int(parsed.network_address)
            out.append(str(ipaddress.IPv6Network((value, 96 + parsed.prefixlen))))
    return out


def allowed_networks(settings, networks, addresses):
    everything = set(networks) | set(addresses) | set(LOOPBACK)
    if settings["allow_local_network"]:
        everything |= set(LOCAL_NETWORKS)
    parsed = [ipaddress.ip_network(net, strict=False) for net in everything]
    v4 = [net for net in parsed if net.version == 4]
    v6 = [net for net in parsed if net.version == 6]
    collapsed = list(ipaddress.collapse_addresses(v4)) + list(ipaddress.collapse_addresses(v6))
    return [str(net) for net in collapsed]


def current_networks(config_path, state):
    """Resolve the allowlist. Returns (networks, fresh, failed, settings) and updates the address memory.

    On an IPv6-only network an IPv4 destination is reached through its NAT64 address, so each
    allowed IPv4 network is also allowed under the NAT64 prefix. The prefix is recorded in the state.
    """
    settings, networks, names = load_config(config_path)
    fresh, failed = resolve(names)
    addresses = remember(state, fresh, settings["keep_addresses_hours"])
    social_networks, social_names = social_members(config_path)
    social_ips = [ip for ip, host in fresh.items() if host in social_names]
    social_addresses = remember_field(state, "social_seen", social_ips, settings["keep_addresses_hours"])
    if not social_networks and not social_names:
        state["social_seen"] = {}
    state["social_networks"] = sorted(social_networks | {
        str(ipaddress.ip_network(ip, strict=False)) for ip in social_addresses})
    prefix = nat64_prefix_for(settings["nat64_prefix"])
    state["nat64_prefix"] = str(prefix) if prefix else None
    extra = synthesize_nat64(list(networks) + addresses, prefix) if prefix else []
    return allowed_networks(settings, set(networks) | set(extra), addresses), fresh, failed, settings


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def process_alive(pid):
    """True if a process with this pid exists. Never signals it on Windows."""
    if SYSTEM == "Windows":
        listing = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True).stdout
        return str(pid) in listing.split()
    try:
        os.kill(pid, 0)  # signal 0 only checks that the process exists
    except PermissionError:
        return True  # alive but owned by root, the normal case for an unprivileged status check
    except OSError:
        return False
    return True


def watcher_pid():
    """PID of a running `watch` started by this tool, or None."""
    try:
        pid = int(PID_FILE.read_text())
    except (OSError, ValueError):
        return None
    if not process_alive(pid):
        return None
    if SYSTEM == "Windows":
        return pid
    # Guard against PID reuse: only trust the pid if it is still our watch command.
    command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
    return pid if "hotspot_guard" in command and " watch" in command else None


def stop_watcher():
    """Ask a running watcher to exit and wait for it. Returns True if one was running."""
    pid = watcher_pid()
    if pid is None:
        PID_FILE.unlink(missing_ok=True)
        return False
    STOP_FILE.write_text("stop")  # polled once a second by the watch loop, on every OS
    deadline = time.time() + 15
    while watcher_pid() is not None and time.time() < deadline:
        time.sleep(0.5)
    if watcher_pid() is not None and SYSTEM != "Windows":
        os.kill(pid, signal.SIGTERM)  # last resort for a stuck watcher
        time.sleep(1)
    STOP_FILE.unlink(missing_ok=True)
    return True


def require_admin():
    if SYSTEM == "Windows":
        import ctypes
        ok = ctypes.windll.shell32.IsUserAnAdmin() != 0
    else:
        ok = os.geteuid() == 0
    if not ok:
        sys.exit("needs root: run with sudo on macOS or Linux, or from an elevated PowerShell on Windows")


def report(failed):
    for host in failed:
        print(f"warning: no address for {host}; it stays blocked")


def plan(config_path, as_json=False):
    networks, fresh, failed, _ = current_networks(config_path, {})
    ordered = sorted(fresh.items(), key=lambda item: (item[1], item[0]))
    if as_json:
        print(json.dumps({
            "addresses": [{"host": host, "ip": ip} for ip, host in ordered],
            "failed": failed,
            "networks": len(networks),
        }))
        return
    for ip, host in ordered:
        print(f"  {host:<36} {ip}")
    report(failed)
    print(f"{len(fresh)} addresses resolved, {len(networks)} networks would be allowed")


def groups(config_path, as_json=False):
    rows = list_groups(config_path)
    if as_json:
        print(json.dumps(rows))
        return
    for row in rows:
        mark = "on " if row["enabled"] else "off"
        print(f"  {mark} {row['name']:<10} {row['domain_count']:>3} entries  {row['description']}")


def install(config_path, dry_run=False):
    backend = firewall.get_backend(STATE_DIR)
    state = load_state()
    networks, _, failed, _ = current_networks(config_path, state)
    if dry_run:
        print(backend.render(networks))
        return
    require_admin()
    backend.apply(networks, state)
    state.update(backend=backend.name, active=True, applied_at=time.time())
    save_state(state)
    (STATE_DIR / usage.BASELINE).unlink(missing_ok=True)
    report(failed)
    print(f"blocking on via {backend.name}: {len(networks)} networks allowed")


def refresh(config_path):
    require_admin()
    state = load_state()
    if not state.get("active"):
        sys.exit("blocking is not on; run apply first")
    backend = firewall.get_backend(STATE_DIR)
    networks, _, failed, _ = current_networks(config_path, state)
    backend.update(networks)
    save_state(state)
    report(failed)
    print(f"{time.strftime('%H:%M:%S')} refreshed: {len(networks)} networks allowed")


USAGE_EVERY = 60  # seconds between usage samples while the watcher runs


def record_usage():
    state = load_state()
    try:
        usage.take_sample(STATE_DIR, state.get("social_networks", []), state.get("nat64_prefix"))
    except Exception as exc:  # usage tracking must never stop the watcher
        print(f"usage sample failed: {exc}")


def usage_report(hours, bucket, sample, as_json):
    if sample:
        require_admin()
        record_usage()
        return
    summary = usage.summarize(STATE_DIR, hours=hours, bucket_seconds=bucket)
    summary["tracking"] = watcher_pid() is not None
    if as_json:
        print(json.dumps(summary))
        return
    totals = summary["totals"]
    print(f"{summary['samples']} samples in the last {hours} hours; tracking {'on' if summary['tracking'] else 'off'}")
    print(f"all traffic: {totals['all']} bytes")
    if totals["social"] is not None:
        print(f"social media: {totals['social']} bytes; excluding social media: {totals['other']} bytes")
    else:
        print("social media split: not available on this system")


def _interrupt(*_):
    raise KeyboardInterrupt  # lets SIGTERM take the same clean exit as Ctrl-C


def watch(config_path):
    existing = watcher_pid()
    if existing is not None and existing != os.getpid():
        sys.exit(f"already watching (pid {existing}); run disable first")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STOP_FILE.unlink(missing_ok=True)  # a stop left over from an earlier run must not end this one
    PID_FILE.write_text(str(os.getpid()))
    signal.signal(signal.SIGTERM, _interrupt)
    try:
        install(config_path)
        interval = load_config(config_path)[0]["refresh_seconds"]
        print(f"refreshing every {interval}s. `disable` stops refreshing; blocking stays on until disabled")
        due = time.time() + interval
        next_usage = time.time()
        while not STOP_FILE.exists():
            time.sleep(1)
            if time.time() >= next_usage:
                record_usage()
                next_usage = time.time() + USAGE_EVERY
            if time.time() < due:
                continue
            due = time.time() + interval
            try:
                refresh(config_path)
            except (Exception, SystemExit) as exc:  # keep going through a bad lookup or a config typo
                print(f"refresh failed, will retry: {exc}")
        print("watch stopped")
    except KeyboardInterrupt:  # Ctrl-C or SIGTERM; rules are only removed by disable itself
        print("\nwatch stopped")
    finally:
        PID_FILE.unlink(missing_ok=True)
        STOP_FILE.unlink(missing_ok=True)


def disable():
    require_admin()
    if stop_watcher():  # stop refreshing before removing rules, so nothing re-applies them
        print("stopped the watcher")
    PID_FILE.unlink(missing_ok=True)
    state = load_state()
    firewall.get_backend(STATE_DIR).disable(state)
    STATE_FILE.unlink(missing_ok=True)
    (STATE_DIR / usage.BASELINE).unlink(missing_ok=True)
    print("blocking off; normal networking restored")


def status(as_json=False):
    state = load_state()
    backend = firewall.get_backend(STATE_DIR)
    pid = watcher_pid()
    if as_json:
        # Unprivileged: reports what this tool recorded. Plain `status` (with sudo) asks the firewall itself.
        print(json.dumps({
            "active": bool(state.get("active")),
            "watching": pid is not None,
            "watcher_pid": pid,
            "applied_at": state.get("applied_at"),
            "remembered": len(state.get("seen", {})),
            "backend": backend.name,
            "log_file": str(LOG_FILE),
            "nat64_prefix": state.get("nat64_prefix"),
        }))
        return
    on = backend.is_active(state)
    print(f"blocking: {'on' if on else 'off'} ({backend.name})")
    print(f"watcher: {f'running (pid {pid})' if pid else 'not running'}")
    if state.get("applied_at"):
        print(f"last applied: {time.ctime(state['applied_at'])}")
    print(f"addresses remembered: {len(state.get('seen', {}))}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Block outbound traffic except an allowlist.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help="allowlist file (default: hotspot-guard.ini beside this script)")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")
    plan_cmd = commands.add_parser("plan", help="resolve the allowlist and print it; changes nothing")
    plan_cmd.add_argument("--json", action="store_true", help="machine-readable output")
    apply_cmd = commands.add_parser("apply", help="turn blocking on")
    apply_cmd.add_argument("--dry-run", action="store_true",
                           help="print the firewall rules instead of installing them")
    commands.add_parser("refresh", help="re-resolve names and update the rules once")
    commands.add_parser("watch", help="apply, then keep the rules refreshed")
    commands.add_parser("disable", help="stop the watcher and remove all hotspot-guard rules")
    status_cmd = commands.add_parser("status", help="show whether blocking is on")
    status_cmd.add_argument("--json", action="store_true", help="unprivileged, machine-readable output")
    groups_cmd = commands.add_parser("groups", help="list allowlist groups")
    groups_cmd.add_argument("--json", action="store_true", help="machine-readable output")
    usage_cmd = commands.add_parser("usage", help="data used over time, with and without social media")
    usage_cmd.add_argument("--hours", type=int, default=24, help="how far back to report")
    usage_cmd.add_argument("--bucket", type=int, default=3600, help="seconds per bucket")
    usage_cmd.add_argument("--sample", action="store_true", help="record a sample now (needs root)")
    usage_cmd.add_argument("--json", action="store_true", help="machine-readable output")
    set_cmd = commands.add_parser("set-group", help="turn an allowlist group on or off in the config")
    set_cmd.add_argument("name")
    set_cmd.add_argument("state", choices=["on", "off"])
    args = parser.parse_args(argv)

    if args.command == "plan":
        plan(args.config, as_json=args.json)
    elif args.command == "apply":
        install(args.config, dry_run=args.dry_run)
    elif args.command == "refresh":
        refresh(args.config)
    elif args.command == "watch":
        watch(args.config)
    elif args.command == "disable":
        disable()
    elif args.command == "status":
        status(as_json=args.json)
    elif args.command == "groups":
        groups(args.config, as_json=args.json)
    elif args.command == "usage":
        usage_report(args.hours, args.bucket, args.sample, args.json)
    else:
        set_group(args.config, args.name, args.state == "on")
        print(f"[{args.name}] {'enabled' if args.state == 'on' else 'disabled'}; run refresh to apply it")


if __name__ == "__main__":
    main()
