"""Firewall backends for hotspot-guard.

Every backend installs the same policy: drop all outbound traffic, then allow
loopback, DNS, DHCP, ping and the destination networks it is given. The rules
live in their own pf anchor, nftables table or Windows Firewall group, so
disabling removes only what hotspot-guard added.
"""

import platform
import subprocess
import sys
from pathlib import Path

PF_ANCHOR = "hotspot_guard"
PF_TABLE = "hg_allowed"
NFT_TABLE = "hotspot_guard"
WIN_GROUP = "HotspotGuard"
WIN_PROFILES = ("Domain", "Private", "Public")
CHUNK = 256  # addresses per nftables statement, to keep lines short


def run(*cmd, check=True):
    """Run a command. With check set, exit with its stderr on failure."""
    try:
        result = subprocess.run(cmd, text=True, capture_output=True)
    except FileNotFoundError:
        if check:
            sys.exit(f"required command not found: {cmd[0]}")
        return subprocess.CompletedProcess(cmd, 127, "", "")
    if check and result.returncode != 0:
        sys.exit(f"{cmd[0]} failed:\n{(result.stderr or result.stdout).strip()}")
    return result


def split_families(networks):
    v4 = [net for net in networks if ":" not in net]
    v6 = [net for net in networks if ":" in net]
    return v4, v6


def chunks(items, size=CHUNK):
    for start in range(0, len(items), size):
        yield items[start:start + size]


# macOS -----------------------------------------------------------------------

PF_ANCHOR_RULES = (
    f"table <{PF_TABLE}> persist\n"
    "pass out quick on lo0 all\n"
    "pass out quick inet proto udp from any port 68 to any port 67 keep state\n"
    "pass out quick proto { tcp udp } to any port 53 keep state\n"
    "pass out quick inet proto icmp all keep state\n"
    "pass out quick inet6 proto icmp6 all keep state\n"
    f"pass out quick to <{PF_TABLE}> keep state\n"
    "block drop out quick all\n"
)


class PfBackend:
    """macOS: a pf anchor with a default-drop outbound rule and a table of allowed addresses."""

    name = "pf"

    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.main_file = state_dir / "pf.conf"
        self.anchor_file = state_dir / "pf-anchor.conf"
        self.table_file = state_dir / "pf-allowed.txt"

    def _main_conf(self):
        # Copy of the system ruleset with our anchor appended. /etc/pf.conf is never edited.
        base = Path("/etc/pf.conf").read_text()
        return (
            base.rstrip() + "\n"
            f'anchor "{PF_ANCHOR}"\n'
            f'load anchor "{PF_ANCHOR}" from "{self.anchor_file}"\n'
        )

    def render(self, networks):
        return (
            f"# {self.main_file}\n{self._main_conf()}\n"
            f"# {self.anchor_file}\n{PF_ANCHOR_RULES}\n"
            f"# {self.table_file} ({len(networks)} entries)\n" + "\n".join(networks) + "\n"
        )

    def apply(self, networks, state):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.anchor_file.write_text(PF_ANCHOR_RULES)
        self.main_file.write_text(self._main_conf())
        run("pfctl", "-n", "-f", str(self.main_file))  # parse only, so a bad file changes nothing
        if "Status: Enabled" in run("pfctl", "-s", "info").stdout:
            state.setdefault("pf_enabled_by_us", False)
        else:
            state.setdefault("pf_enabled_by_us", True)
            run("pfctl", "-e")
        run("pfctl", "-f", str(self.main_file))
        self.update(networks)

    def update(self, networks):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.table_file.write_text("\n".join(networks) + "\n")
        run("pfctl", "-a", PF_ANCHOR, "-t", PF_TABLE, "-T", "replace", "-f", str(self.table_file))

    def disable(self, state):
        run("pfctl", "-a", PF_ANCHOR, "-F", "all", check=False)
        run("pfctl", "-a", PF_ANCHOR, "-t", PF_TABLE, "-T", "flush", check=False)
        run("pfctl", "-f", "/etc/pf.conf", check=False)
        if state.get("pf_enabled_by_us"):
            run("pfctl", "-d", check=False)

    def is_active(self, state):
        return "block drop" in run("pfctl", "-a", PF_ANCHOR, "-s", "rules", check=False).stdout


# Linux -----------------------------------------------------------------------


class NftBackend:
    """Linux: a dedicated inet table whose output chain drops anything not allowed."""

    name = "nftables"

    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.script_file = state_dir / "nft-rules.nft"

    def _elements(self, networks):
        v4, v6 = split_families(networks)
        lines = []
        for set_name, nets in (("allow4", v4), ("allow6", v6)):
            for part in chunks(nets):
                lines.append(f"add element inet {NFT_TABLE} {set_name} {{ {', '.join(part)} }}")
        return lines

    def render(self, networks):
        header = [
            f"add table inet {NFT_TABLE}",  # create if missing, so the delete below cannot fail
            f"delete table inet {NFT_TABLE}",
            f"table inet {NFT_TABLE} {{",
            "  set allow4 { type ipv4_addr; flags interval; }",
            "  set allow6 { type ipv6_addr; flags interval; }",
            "  chain output {",
            "    type filter hook output priority 0; policy drop;",
            "    oif lo accept",
            "    ct state established,related accept",
            "    udp dport { 53, 67 } accept",
            "    tcp dport 53 accept",
            "    ip protocol icmp accept",
            "    ip6 nexthdr icmpv6 accept",
            "    ip daddr @allow4 accept",
            "    ip6 daddr @allow6 accept",
            "  }",
            "}",
        ]
        return "\n".join(header + self._elements(networks)) + "\n"

    def render_update(self, networks):
        flush = [f"flush set inet {NFT_TABLE} {name}" for name in ("allow4", "allow6")]
        return "\n".join(flush + self._elements(networks)) + "\n"

    def _load(self, text):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.script_file.write_text(text)
        run("nft", "-c", "-f", str(self.script_file))  # check syntax first, so a bad file changes nothing
        run("nft", "-f", str(self.script_file))

    def apply(self, networks, state):
        self._load(self.render(networks))

    def update(self, networks):
        self._load(self.render_update(networks))

    def disable(self, state):
        run("nft", "delete", "table", "inet", NFT_TABLE, check=False)

    def is_active(self, state):
        return run("nft", "list", "table", "inet", NFT_TABLE, check=False).returncode == 0


# Windows ---------------------------------------------------------------------

WIN_EXTRA_RULES = [
    "-DisplayName 'HotspotGuard DNS (UDP)' -Protocol UDP -RemotePort 53",
    "-DisplayName 'HotspotGuard DNS (TCP)' -Protocol TCP -RemotePort 53",
    "-DisplayName 'HotspotGuard DHCP' -Protocol UDP -LocalPort 68 -RemotePort 67",
    "-DisplayName 'HotspotGuard ICMPv4' -Protocol ICMPv4",
    "-DisplayName 'HotspotGuard ICMPv6' -Protocol ICMPv6",
]


class WindowsBackend:
    """Windows: outbound default set to Block, with allow rules kept in one firewall group."""

    name = "windows-firewall"

    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.script_file = state_dir / "apply.ps1"

    def render(self, networks):
        addresses = ",".join(f"'{net}'" for net in networks)
        lines = [
            "$ErrorActionPreference = 'Stop'",
            f"$group = '{WIN_GROUP}'",
            "Get-NetFirewallRule -Group $group -ErrorAction SilentlyContinue | Remove-NetFirewallRule",
            "New-NetFirewallRule -Group $group -Direction Outbound -Action Allow "
            f"-DisplayName 'HotspotGuard allowed destinations' -RemoteAddress @({addresses}) | Out-Null",
        ]
        lines += [
            f"New-NetFirewallRule -Group $group -Direction Outbound -Action Allow {rule} | Out-Null"
            for rule in WIN_EXTRA_RULES
        ]
        lines.append(
            f"Set-NetFirewallProfile -Profile {','.join(WIN_PROFILES)} -DefaultOutboundAction Block"
        )
        return "\n".join(lines) + "\n"

    def _ps_file(self, text):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.script_file.write_text(text, encoding="utf-8")
        run("powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(self.script_file))

    def _ps(self, command, check=True):
        return run("powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                   "-Command", command, check=check)

    def _read_profiles(self):
        out = self._ps('Get-NetFirewallProfile | ForEach-Object { "$($_.Name)=$($_.DefaultOutboundAction)" }').stdout
        return dict(line.split("=", 1) for line in out.split() if "=" in line)

    def apply(self, networks, state):
        if "windows_profiles" not in state:  # remember the user's original setting, once
            state["windows_profiles"] = self._read_profiles()
        self._ps_file(self.render(networks))

    def update(self, networks):
        self._ps_file(self.render(networks))

    def disable(self, state):
        self._ps(f"Get-NetFirewallRule -Group '{WIN_GROUP}' -ErrorAction SilentlyContinue | Remove-NetFirewallRule",
                 check=False)
        profiles = state.get("windows_profiles") or {}
        for name in WIN_PROFILES:
            action = profiles.get(name, "Allow")
            if action not in ("Allow", "Block"):  # NotConfigured behaves as Allow
                action = "Allow"
            self._ps(f"Set-NetFirewallProfile -Profile {name} -DefaultOutboundAction {action}", check=False)

    def is_active(self, state):
        out = self._ps(f"@(Get-NetFirewallRule -Group '{WIN_GROUP}' -ErrorAction SilentlyContinue).Count",
                       check=False).stdout.strip()
        return out.isdigit() and int(out) > 0


def get_backend(state_dir):
    backends = {"Darwin": PfBackend, "Linux": NftBackend, "Windows": WindowsBackend}
    system = platform.system()
    if system not in backends:
        sys.exit(f"unsupported platform: {system}")
    return backends[system](state_dir)
