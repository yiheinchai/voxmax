#!/usr/bin/env python3
"""hotspot-guard: allow only listed destinations while on a metered connection.

Installs default-deny outbound rules with the OS firewall: pf on macOS,
nftables on Linux, Windows Defender Firewall on Windows. The allowed
destinations are the addresses the allowlist names resolve to, refreshed
while `watch` runs. Standard library only, Python 3.8 or newer.

  plan      resolve the allowlist and print it (changes nothing)
  apply     turn blocking on (root or administrator)
  refresh   re-resolve the allowlist and update the live rules once
  watch     apply, then refresh every refresh_seconds until Ctrl-C
  disable   remove every hotspot-guard rule and restore normal networking
  status    report whether blocking is on
"""

import argparse
import configparser
import ipaddress
import json
import os
import platform
import re
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import firewall

SYSTEM = platform.system()
HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "hotspot-guard.ini"
STATE_DIR = {
    "Darwin": Path("/var/db/hotspot-guard"),
    "Linux": Path("/var/lib/hotspot-guard"),
    "Windows": Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "hotspot-guard",
}.get(SYSTEM, Path.home() / ".hotspot-guard")
STATE_FILE = STATE_DIR / "state.json"

LOOPBACK = ["127.0.0.0/8", "::1/128"]
LOCAL_NETWORKS = [
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",  # private IPv4, incl. the hotspot subnet
    "224.0.0.0/4", "fc00::/7", "fe80::/10", "ff00::/8",                 # multicast and IPv6 link-local or ULA
]
HOSTNAME_RE = re.compile(r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def load_config(path):
    """Return (settings, networks, names) for the enabled groups in the config file."""
    parser = configparser.ConfigParser(interpolation=None)
    if not parser.read(path, encoding="utf-8"):
        sys.exit(f"config file not found: {path}")
    settings = {
        "allow_local_network": parser.getboolean("settings", "allow_local_network", fallback=True),
        "refresh_seconds": max(60, parser.getint("settings", "refresh_seconds", fallback=300)),
        "keep_addresses_hours": parser.getfloat("settings", "keep_addresses_hours", fallback=6.0),
    }
    networks, names = set(), set()
    for section in parser.sections():
        if section == "settings" or not parser.getboolean(section, "enabled", fallback=False):
            continue
        for entry in parser.get(section, "domains", fallback="").split():
            entry = entry.lower().rstrip(".")
            try:
                networks.add(str(ipaddress.ip_network(entry, strict=False)))
            except ValueError:
                if not HOSTNAME_RE.match(entry):
                    sys.exit(f"[{section}] not a domain, IP address or CIDR range: {entry}")
                names.add(entry)
    return settings, networks, names


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
    now = time.time()
    seen = {ip: seen_at for ip, seen_at in state.get("seen", {}).items() if now - seen_at < keep_hours * 3600}
    seen.update(dict.fromkeys(fresh, now))
    state["seen"] = seen
    return list(seen)


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
    """Resolve the allowlist. Returns (networks, fresh, failed, settings) and updates the address memory."""
    settings, networks, names = load_config(config_path)
    fresh, failed = resolve(names)
    addresses = remember(state, fresh, settings["keep_addresses_hours"])
    return allowed_networks(settings, networks, addresses), fresh, failed, settings


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


def plan(config_path):
    networks, fresh, failed, settings = current_networks(config_path, {})
    for ip, host in sorted(fresh.items(), key=lambda item: (item[1], item[0])):
        print(f"  {host:<36} {ip}")
    report(failed)
    print(f"{len(fresh)} addresses resolved, {len(networks)} networks would be allowed")


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


def watch(config_path):
    install(config_path)
    interval = load_config(config_path)[0]["refresh_seconds"]
    print(f"refreshing every {interval}s. Ctrl-C stops refreshing; blocking stays on until: disable")
    try:
        while True:
            time.sleep(interval)
            try:
                refresh(config_path)
            except (Exception, SystemExit) as exc:  # keep going through a bad lookup or a config typo
                print(f"refresh failed, will retry: {exc}")
    except KeyboardInterrupt:
        print("\nstopped refreshing; blocking is still on")


def disable():
    require_admin()
    state = load_state()
    firewall.get_backend(STATE_DIR).disable(state)
    STATE_FILE.unlink(missing_ok=True)
    print("blocking off; normal networking restored")


def status():
    state = load_state()
    backend = firewall.get_backend(STATE_DIR)
    on = backend.is_active(state)
    print(f"blocking: {'on' if on else 'off'} ({backend.name})")
    if state.get("applied_at"):
        print(f"last applied: {time.ctime(state['applied_at'])}")
    print(f"addresses remembered: {len(state.get('seen', {}))}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Block outbound traffic except an allowlist.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help="allowlist file (default: hotspot-guard.ini beside this script)")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")
    commands.add_parser("plan", help="resolve the allowlist and print it; changes nothing")
    apply_cmd = commands.add_parser("apply", help="turn blocking on")
    apply_cmd.add_argument("--dry-run", action="store_true",
                           help="print the firewall rules instead of installing them")
    commands.add_parser("refresh", help="re-resolve names and update the rules once")
    commands.add_parser("watch", help="apply, then keep the rules refreshed")
    commands.add_parser("disable", help="remove all hotspot-guard rules")
    commands.add_parser("status", help="show whether blocking is on")
    args = parser.parse_args(argv)

    if args.command == "plan":
        plan(args.config)
    elif args.command == "apply":
        install(args.config, dry_run=args.dry_run)
    elif args.command == "refresh":
        refresh(args.config)
    elif args.command == "watch":
        watch(args.config)
    elif args.command == "disable":
        disable()
    else:
        status()


if __name__ == "__main__":
    main()
