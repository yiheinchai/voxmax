"""Data-usage tracking for hotspot-guard.

Totals come from the byte counters of the default network interface, which every supported
system exposes. The social-media split comes from per-connection byte counters, where the
system has them: Linux conntrack accounting and the macOS pf state table. Windows only has
interface totals, so there the split is reported as unavailable.

A sample is the change since the previous sample. The baseline is kept in the state folder,
so separate `usage --sample` runs and watcher restarts keep counting from the right place.
Standard library only.
"""

import ipaddress
import json
import platform
import re
import subprocess
import time
from pathlib import Path

SYSTEM = platform.system()
RETENTION_SECONDS = 30 * 86400
PRUNE_OVER_BYTES = 2_000_000
HISTORY = "usage.jsonl"
BASELINE = "usage-baseline.json"
CONNTRACK = Path("/proc/net/nf_conntrack")
CONNTRACK_ACCT = Path("/proc/sys/net/netfilter/nf_conntrack_acct")

CONN_RE = re.compile(
    r"src=(\S+) dst=(\S+) sport=(\d+) dport=(\d+) (?:packets=\d+ )?bytes=(\d+) "
    r"src=(\S+) dst=(\S+) sport=(\d+) dport=(\d+) (?:packets=\d+ )?bytes=(\d+)")
PROTO_RE = re.compile(r"\s(tcp|udp|icmp|icmpv6|sctp|dccp)\s")
PF_STATE_RE = re.compile(r"^(\S+)\s+(tcp|udp|icmp\S*)\s+(\S+)\s+->\s+(\S+)")
PF_BYTES_RE = re.compile(r"Bytes:\s*In:\s*(\d+)\s+Out:\s*(\d+)")


# --- Reading the system -------------------------------------------------------------------

def _run(*cmd):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def parse_route(text):
    """The interface of the default route in /proc/net/route, or None."""
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) > 1 and fields[1] == "00000000":
            return fields[0]
    return None


def parse_proc_net_dev(text, name):
    """(received, sent) bytes for one interface from /proc/net/dev, or None."""
    for line in text.splitlines():
        if ":" not in line:
            continue
        iface, rest = line.split(":", 1)
        if iface.strip() == name:
            fields = rest.split()
            return int(fields[0]), int(fields[8])
    return None


def parse_netstat_ibi(text, name):
    """(received, sent) bytes from `netstat -bI <name>` on macOS, or None."""
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 10 and fields[0] == name and fields[2].startswith("<Link"):
            return int(fields[6]), int(fields[9])
    return None


def parse_netstat_e(text):
    """(received, sent) totals from `netstat -e` on Windows, or None."""
    match = re.search(r"Bytes\s+(\d+)\s+(\d+)", text)
    return (int(match.group(1)), int(match.group(2))) if match else None


def parse_conntrack(text):
    """{key: (remote_ip, bytes)} from /proc/net/nf_conntrack.

    Returns None when the lines carry no byte counts, which means accounting is off.
    """
    if text.strip() and "bytes=" not in text:
        return None
    counters = {}
    for line in text.splitlines():
        match = CONN_RE.search(line)
        proto = PROTO_RE.search(line)
        if not match or not proto:
            continue
        src, dst, sport, dport, orig_bytes, _, _, _, _, reply_bytes = match.groups()
        counters[f"{proto.group(1)} {src}:{sport} {dst}:{dport}"] = (dst, int(orig_bytes) + int(reply_bytes))
    return counters


def _pf_endpoint(token):
    """(address, port) from pf's display: addr:port, or addr[port] for IPv6."""
    if token.endswith("]") and "[" in token:
        address, port = token[:-1].rsplit("[", 1)
        return address, port
    address, _, port = token.rpartition(":")
    return address, port


def parse_pf_states(text):
    """{key: (remote_ip, bytes)} from `pfctl -ss -vv`. The layout is handled best-effort: it has
    not been checked against a Mac yet, so an unrecognised layout yields no split."""
    counters = {}
    current = None
    for line in text.splitlines():
        state = PF_STATE_RE.match(line.strip())
        if state and not line.startswith((" ", "\t")):
            proto, source, destination = state.group(2), state.group(3), state.group(4)
            current = f"{proto} {source} {destination}"
            counters[current] = (_pf_endpoint(destination)[0], 0)
        elif current:
            found = PF_BYTES_RE.search(line)
            if found:
                counters[current] = (counters[current][0], int(found.group(1)) + int(found.group(2)))
    return counters


def default_interface():
    if SYSTEM == "Linux":
        try:
            return parse_route(Path("/proc/net/route").read_text())
        except OSError:
            return None
    if SYSTEM == "Darwin":
        match = re.search(r"interface:\s*(\S+)", _run("route", "-n", "get", "default"))
        return match.group(1) if match else None
    return None  # Windows: the totals cover every adapter


def interface_bytes(name):
    if SYSTEM == "Linux" and name:
        try:
            return parse_proc_net_dev(Path("/proc/net/dev").read_text(), name)
        except OSError:
            return None
    if SYSTEM == "Darwin" and name:
        return parse_netstat_ibi(_run("netstat", "-bI", name), name)
    if SYSTEM == "Windows":
        return parse_netstat_e(_run("netstat", "-e"))
    return None


def connection_counters():
    """Per-connection byte counters, or None where the system does not provide them."""
    if SYSTEM == "Linux":
        try:
            if CONNTRACK_ACCT.read_text().strip() != "1":
                CONNTRACK_ACCT.write_text("1")  # turn accounting on; only new connections are counted
        except OSError:
            pass
        try:
            return parse_conntrack(CONNTRACK.read_text())
        except OSError:
            return None
    if SYSTEM == "Darwin":
        text = _run("pfctl", "-ss", "-vv")
        return parse_pf_states(text) if text else None
    return None


# --- Attribution and storage --------------------------------------------------------------

def _is_social(address, networks, nat64_prefix):
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.version == 6 and nat64_prefix is not None and ip in nat64_prefix:
        ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)  # an IPv4 site reached through NAT64
    return any(ip in net for net in networks)


def attribute(connections, previous, social_networks, nat64_prefix=None):
    """(social_bytes, other_bytes) for the traffic since `previous`."""
    networks = [ipaddress.ip_network(net, strict=False) for net in social_networks]
    social = other = 0
    for key, (remote, total) in connections.items():
        delta = total - previous.get(key, 0)
        if delta < 0:  # the counter was reset, so everything counted is new
            delta = total
        if _is_social(remote, networks, nat64_prefix):
            social += delta
        else:
            other += delta
    return social, other


def _load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _append(path, row):
    with path.open("a") as handle:
        handle.write(json.dumps(row) + "\n")
    if path.stat().st_size > PRUNE_OVER_BYTES:
        _prune(path)


def _prune(path, now=None):
    cutoff = (time.time() if now is None else now) - RETENTION_SECONDS
    kept = []
    for line in path.read_text().splitlines():
        try:
            if json.loads(line)["t"] >= cutoff:
                kept.append(line)
        except (ValueError, KeyError):
            continue
    path.write_text("".join(line + "\n" for line in kept))


def take_sample(directory, social_networks=(), nat64_prefix=None, now=None):
    """Records the traffic since the last sample. Returns the new row, or None for the first sample."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    now = time.time() if now is None else now
    if isinstance(nat64_prefix, str):
        nat64_prefix = ipaddress.IPv6Network(nat64_prefix) if nat64_prefix else None
    baseline_path = directory / BASELINE
    baseline = _load_json(baseline_path, {})
    iface = default_interface()
    counters = interface_bytes(iface)
    connections = connection_counters()
    row = None
    if counters and baseline.get("iface") == iface and "rx" in baseline:
        d_rx = max(0, counters[0] - baseline["rx"])
        d_tx = max(0, counters[1] - baseline["tx"])
        total = d_rx + d_tx
        social = other = None
        method = "none"
        if connections is not None:
            previous = baseline.get("conns", {})
            social_bytes, other_bytes = attribute(connections, previous, social_networks, nat64_prefix)
            attributed = social_bytes + other_bytes
            if attributed > 0:
                social = round(total * social_bytes / attributed)
                other = total - social
                method = "conntrack" if SYSTEM == "Linux" else "pf"
        row = {"t": round(now), "iface": iface, "rx": d_rx, "tx": d_tx, "total": total,
               "social": social, "other": other, "method": method}
        _append(directory / HISTORY, row)
    if counters:
        conns = {key: value[1] for key, value in (connections or {}).items()}
        _write_json(baseline_path, {"iface": iface, "rx": counters[0], "tx": counters[1], "conns": conns})
    return row


def _write_json(path, data):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def read_rows(directory, since):
    try:
        lines = (Path(directory) / HISTORY).read_text().splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("t", 0) >= since:
            rows.append(row)
    return rows


def summarize(directory, hours=24, bucket_seconds=3600, now=None):
    """Totals and buckets for the chart. Social and other bytes are only known for samples that
    had a split, so `split_coverage` says how much of the total they cover."""
    now = time.time() if now is None else now
    rows = read_rows(directory, now - hours * 3600)
    buckets = {}
    totals = {"all": 0, "social": 0, "other": 0, "split": 0}
    methods = set()
    for row in rows:
        start = row["t"] // bucket_seconds * bucket_seconds
        bucket = buckets.setdefault(start, {"start": start, "total": 0, "social": 0, "other": 0, "split": 0})
        bucket["total"] += row["total"]
        totals["all"] += row["total"]
        if row["social"] is not None:
            for target in (bucket, totals):
                target["split"] += row["total"]
                target["social"] += row["social"]
                target["other"] += row["other"]
            methods.add(row["method"])
    coverage = totals["split"] / totals["all"] if totals["all"] else None
    return {
        "hours": hours,
        "bucket_seconds": bucket_seconds,
        "samples": len(rows),
        "last_sample": rows[-1]["t"] if rows else None,
        "methods": sorted(methods),
        "totals": {
            "all": totals["all"],
            "social": totals["social"] if totals["split"] else None,
            "other": totals["other"] if totals["split"] else None,
            "split_coverage": coverage,
        },
        "buckets": [buckets[start] for start in sorted(buckets)],
    }
