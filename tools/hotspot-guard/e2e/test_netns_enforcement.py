"""End-to-end firewall tests on Linux, using network namespaces. Needs root, iproute2 and nftables.

Topology (everything runs on this machine, in separate network namespaces):

    hg-client  ---- veth ----  hg-hotspot
    (runs hotspot_guard)       (stands in for the phone's hotspot and the internet)

The hotspot owns the test "internet" addresses on its loopback and runs one listener on
all of them. A probe from the client that connects and gets a reply means the packets were
allowed. A probe that times out means the firewall dropped them.

Run from tools/hotspot-guard:  sudo python3 -m unittest discover -s e2e -v
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
ENGINE = HERE / "hotspot_guard.py"
SERVE = Path(__file__).resolve().parent / "serve.py"
PROBE = Path(__file__).resolve().parent / "probe.py"
DOWNLOAD = Path(__file__).resolve().parent / "download.py"
BULK_PORT = 8081
BULK_BYTES = 1_000_000
CLIENT, HOTSPOT = "hg-client", "hg-hotspot"
PORT = 8080

# Test addresses. 203.0.113.0/24 and 2001:db8::/32 are documentation ranges, so nothing real is touched.
ALLOWED_V4, BLOCKED_V4 = "203.0.113.10", "203.0.113.20"
OTHER_V4 = "203.0.113.30"  # an ordinary site, allowed in the usage test to compare with the social one
ALLOWED_V6, BLOCKED_V6 = "2001:db8:77::10", "2001:db8:77::20"
NAT64_PREFIX = "64:ff9b::/96"
ALLOWED_NAT64 = "64:ff9b::cb00:710a"   # 203.0.113.10 under the NAT64 prefix
BLOCKED_NAT64 = "64:ff9b::cb00:7114"   # 203.0.113.20 under the NAT64 prefix
LOCAL_PEER = "10.77.0.1"               # the hotspot end of the link, which is on the local network


def run(*cmd, check=True, timeout=60):
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {result.stderr or result.stdout}")
    return result


def in_client(*cmd, check=True, timeout=60):
    return run("ip", "netns", "exec", CLIENT, *cmd, check=check, timeout=timeout)


def engine(config, *args, check=True):
    return in_client("python3", "-B", str(ENGINE), "--config", str(config), *args, check=check, timeout=120)


def reachable(address, timeout=2):
    """True if a TCP connection from the client to the hotspot listener succeeds and gets a reply."""
    result = in_client("python3", "-B", str(PROBE), address, str(PORT), str(timeout), check=False,
                       timeout=timeout + 10)
    return result.returncode == 0


def tools_available():
    if os.geteuid() != 0:
        return "needs root"
    for tool in ("ip", "nft", "python3"):
        if shutil.which(tool) is None:
            return f"{tool} is not installed"
    return None


SKIP_REASON = tools_available()


@unittest.skipIf(SKIP_REASON, SKIP_REASON or "")
class FirewallEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workdir = Path(tempfile.mkdtemp(prefix="hg-e2e-"))
        cls.config = cls.workdir / "hotspot-guard.ini"
        os.environ["HOTSPOT_GUARD_STATE_DIR"] = str(cls.workdir / "state")  # keep real state out of the tests
        cls.server = None
        cls.built = False
        try:
            cls._build_topology()
            cls._write_config(allowed=[ALLOWED_V4, ALLOWED_V6])
            cls.server = subprocess.Popen(
                ["ip", "netns", "exec", HOTSPOT, "python3", "-B", str(SERVE), str(PORT)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            cls.bulk = subprocess.Popen(
                ["ip", "netns", "exec", HOTSPOT, "python3", "-B", str(SERVE), str(BULK_PORT), str(BULK_BYTES)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            cls._wait_for_listener()
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        if cls.built:
            engine(cls.config, "disable", check=False)
        for listener in (getattr(cls, "server", None), getattr(cls, "bulk", None)):
            if listener is not None:
                listener.kill()
                listener.wait()
        for ns in (CLIENT, HOTSPOT):
            run("ip", "netns", "del", ns, check=False)
        os.environ.pop("HOTSPOT_GUARD_STATE_DIR", None)
        shutil.rmtree(cls.workdir, ignore_errors=True)

    @classmethod
    def _build_topology(cls):
        for ns in (CLIENT, HOTSPOT):
            run("ip", "netns", "add", ns)
        cls.built = True  # from here on, teardown must clean up
        run("ip", "link", "add", "hgc", "type", "veth", "peer", "name", "hgh")
        run("ip", "link", "set", "hgc", "netns", CLIENT)
        run("ip", "link", "set", "hgh", "netns", HOTSPOT)
        run("ip", "-n", CLIENT, "addr", "add", "10.77.0.2/24", "dev", "hgc")
        run("ip", "-n", HOTSPOT, "addr", "add", LOCAL_PEER + "/24", "dev", "hgh")
        for ns in (CLIENT, HOTSPOT):
            run("ip", "-n", ns, "link", "set", "lo", "up")
            run("ip", "-n", ns, "link", "set", "hgc" if ns == CLIENT else "hgh", "up")
        run("ip", "-n", CLIENT, "route", "add", "203.0.113.0/24", "via", LOCAL_PEER)
        run("ip", "-n", CLIENT, "route", "add", "default", "via", LOCAL_PEER)  # the hotspot is the default route
        for address in (ALLOWED_V4, BLOCKED_V4, OTHER_V4):
            run("ip", "-n", HOTSPOT, "addr", "add", address + "/32", "dev", "lo")
        # IPv6 is optional: some kernels (and sandboxes) have none, and the tests skip those cases.
        cls.ipv6 = run("ip", "-n", CLIENT, "-6", "addr", "add", "fd77::2/64", "dev", "hgc", "nodad",
                       check=False).returncode == 0
        if cls.ipv6:
            run("ip", "-n", HOTSPOT, "-6", "addr", "add", "fd77::1/64", "dev", "hgh", "nodad")
            run("ip", "-n", CLIENT, "-6", "route", "add", "2001:db8:77::/64", "via", "fd77::1")
            run("ip", "-n", CLIENT, "-6", "route", "add", NAT64_PREFIX, "via", "fd77::1")
            for address in (ALLOWED_V6, BLOCKED_V6, ALLOWED_NAT64, BLOCKED_NAT64):
                run("ip", "-n", HOTSPOT, "-6", "addr", "add", address + "/128", "dev", "lo", "nodad")

    def require_ipv6(self):
        if not self.ipv6:
            self.skipTest("this kernel has no IPv6, so IPv6 and NAT64 cases cannot run here")

    @classmethod
    def _wait_for_listener(cls):
        deadline = time.time() + 10
        while time.time() < deadline:
            if reachable(ALLOWED_V4):
                return
            time.sleep(0.2)
        raise RuntimeError("the test listener never answered; the topology is broken")

    @classmethod
    def _write_config(cls, allowed, extra_settings=""):
        if not cls.ipv6:
            allowed = [a for a in allowed if ":" not in a]
        names = "\n".join(f"    {address}" for address in allowed)
        cls.config.write_text(
            "[settings]\n"
            "allow_local_network = yes\n"
            f"nat64_prefix = {NAT64_PREFIX}\n"
            f"{extra_settings}"
            "\n"
            "[custom]\n"
            "enabled = yes\n"
            "domains =\n"
            f"{names}\n")

    # The tests share one topology, so they run in a fixed order.

    def test_01_before_blocking_every_destination_answers(self):
        addresses = [ALLOWED_V4, BLOCKED_V4] + ([ALLOWED_V6, BLOCKED_V6, ALLOWED_NAT64, BLOCKED_NAT64] if self.ipv6 else [])
        for address in addresses:
            self.assertTrue(reachable(address), f"{address} should answer with no blocking")

    def test_02_apply_drops_unlisted_and_keeps_listed_destinations(self):
        engine(self.config, "apply")
        self.assertTrue(reachable(ALLOWED_V4), "listed IPv4 destination must be reachable")
        self.assertFalse(reachable(BLOCKED_V4), "unlisted IPv4 destination must be dropped")
        if self.ipv6:
            self.assertTrue(reachable(ALLOWED_V6), "listed IPv6 destination must be reachable")
            self.assertFalse(reachable(BLOCKED_V6), "unlisted IPv6 destination must be dropped")

    def test_03_local_network_stays_reachable(self):
        self.assertTrue(reachable(LOCAL_PEER), "the hotspot itself must stay reachable while blocking")

    def test_04_nat64_addresses_follow_the_ipv4_allowlist(self):
        self.require_ipv6()
        self.assertTrue(reachable(ALLOWED_NAT64), "NAT64 form of a listed IPv4 destination must be reachable")
        self.assertFalse(reachable(BLOCKED_NAT64), "NAT64 form of an unlisted IPv4 destination must be dropped")

    def test_05_status_reports_the_recorded_state(self):
        status = engine(self.config, "status", "--json").stdout
        self.assertIn('"active": true', status)
        self.assertIn(f'"nat64_prefix": "{NAT64_PREFIX}"', status)

    def test_06_refresh_picks_up_an_edited_allowlist(self):
        self._write_config(allowed=[ALLOWED_V4, ALLOWED_V6, BLOCKED_V4])
        self.assertFalse(reachable(BLOCKED_V4), "still dropped before the refresh")
        engine(self.config, "refresh")
        self.assertTrue(reachable(BLOCKED_V4), "newly listed destination must be reachable after refresh")
        self._write_config(allowed=[ALLOWED_V4, ALLOWED_V6])
        engine(self.config, "refresh")
        self.assertFalse(reachable(BLOCKED_V4), "removing a destination must block it again")

    def test_07_watch_runs_until_disable_stops_it(self):
        log = self.workdir / "watch.log"
        with open(log, "w") as out:
            watcher = subprocess.Popen(
                ["ip", "netns", "exec", CLIENT, "python3", "-B", str(ENGINE), "--config", str(self.config), "watch"],
                stdout=out, stderr=subprocess.STDOUT)
        try:
            deadline = time.time() + 20
            while time.time() < deadline and '"watching": true' not in engine(self.config, "status", "--json").stdout:
                time.sleep(0.3)
            self.assertIn('"watching": true', engine(self.config, "status", "--json").stdout)
            engine(self.config, "disable")
            self.assertEqual(watcher.wait(timeout=20), 0, "watch should exit cleanly on disable")
        finally:
            if watcher.poll() is None:
                watcher.kill()
                watcher.wait()
        self.assertIn("watch stopped", log.read_text())
        self.assertIn('"active": false', engine(self.config, "status", "--json").stdout)

    def test_08_disable_restores_normal_networking(self):
        self._write_config(allowed=[ALLOWED_V4, ALLOWED_V6])
        engine(self.config, "apply")
        self.assertFalse(reachable(BLOCKED_V4))
        engine(self.config, "disable")
        self.assertTrue(reachable(BLOCKED_V4), "IPv4 must be reachable again after disable")
        if self.ipv6:
            self.assertTrue(reachable(BLOCKED_V6), "IPv6 must be reachable again after disable")
            self.assertTrue(reachable(BLOCKED_NAT64), "NAT64 must be reachable again after disable")
        tables = in_client("nft", "list", "tables").stdout
        self.assertNotIn("hotspot_guard", tables, "no hotspot-guard table may be left behind")

    def test_09_usage_splits_social_traffic_from_the_rest(self):
        # A 1 MB download from a social address and one from an ordinary address, both allowed.
        config = self.workdir / "usage.ini"
        config.write_text(
            "[settings]\nallow_local_network = yes\n"
            "[social]\nenabled = yes\ndomains =\n    " + ALLOWED_V4 + "\n"
            "[custom]\nenabled = yes\ndomains =\n    " + OTHER_V4 + "\n")
        engine(config, "apply")
        try:
            engine(config, "usage", "--sample")  # the baseline: the first sample only records the counters
            social = int(in_client("python3", "-B", str(DOWNLOAD), ALLOWED_V4, str(BULK_PORT), timeout=60).stdout)
            other = int(in_client("python3", "-B", str(DOWNLOAD), OTHER_V4, str(BULK_PORT), timeout=60).stdout)
            self.assertEqual((social, other), (BULK_BYTES, BULK_BYTES), "both downloads should complete")
            engine(config, "usage", "--sample")
            report = json.loads(engine(config, "usage", "--json", "--hours", "1").stdout)
        finally:
            engine(config, "disable")
        totals = report["totals"]
        self.assertEqual(report["methods"], ["conntrack"])
        self.assertGreaterEqual(totals["all"], 2 * BULK_BYTES, "the interface counters must see both downloads")
        self.assertGreaterEqual(totals["social"], 0.9 * BULK_BYTES, "social traffic must be attributed to social")
        self.assertGreaterEqual(totals["other"], 0.9 * BULK_BYTES, "other traffic must be left out of social")
        self.assertGreater(totals["social"] / (totals["social"] + totals["other"]), 0.4)
        self.assertLess(totals["social"] / (totals["social"] + totals["other"]), 0.6)
        self.assertGreaterEqual(totals["split_coverage"], 0.9)


if __name__ == "__main__":
    unittest.main()
