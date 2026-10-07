"""Tests for hotspot-guard. Run from this directory: python3 -m unittest -v"""

import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import firewall
import hotspot_guard as hg

NETS = ["127.0.0.0/8", "::1/128", "1.2.3.4/32", "2001:db8::/32"]


class ConfigTests(unittest.TestCase):
    def write_config(self, text):
        directory = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(directory, ignore_errors=True))
        path = Path(directory) / "test.ini"
        path.write_text(text)
        return path

    def test_only_enabled_groups_count(self):
        path = self.write_config(
            "[settings]\n"
            "allow_local_network = no\n"
            "\n"
            "[on]\n"
            "enabled = yes\n"
            "domains =\n"
            "    Example.com.\n"
            "    # a comment inside the list\n"
            "    203.0.113.7\n"
            "    198.51.100.0/24\n"
            "\n"
            "[off]\n"
            "enabled = no\n"
            "domains =\n"
            "    blocked.example\n"
        )
        settings, networks, names = hg.load_config(path)
        self.assertEqual(names, {"example.com"})
        self.assertEqual(networks, {"203.0.113.7/32", "198.51.100.0/24"})
        self.assertFalse(settings["allow_local_network"])

    def test_rejects_bad_entry(self):
        path = self.write_config("[x]\nenabled = yes\ndomains =\n    not a domain\n")
        with self.assertRaises(SystemExit):
            hg.load_config(path)

    def test_refresh_has_a_floor(self):
        path = self.write_config("[settings]\nrefresh_seconds = 5\n")
        self.assertEqual(hg.load_config(path)[0]["refresh_seconds"], 60)

    def test_shipped_config_parses(self):
        settings, networks, names = hg.load_config(hg.DEFAULT_CONFIG)
        self.assertIn("api.anthropic.com", names)
        self.assertIn("www.instagram.com", names)
        self.assertNotIn("github.com", names)  # dev group is off by default


class NetworkTests(unittest.TestCase):
    def test_addresses_inside_a_range_are_collapsed(self):
        nets = hg.allowed_networks({"allow_local_network": False}, {"10.0.0.0/8"}, ["10.1.2.3", "fe80::1"])
        self.assertIn("10.0.0.0/8", nets)
        self.assertNotIn("10.1.2.3/32", nets)
        self.assertIn("fe80::1/128", nets)
        self.assertIn("127.0.0.0/8", nets)
        self.assertIn("::1/128", nets)

    def test_local_network_switch(self):
        on = hg.allowed_networks({"allow_local_network": True}, set(), [])
        off = hg.allowed_networks({"allow_local_network": False}, set(), [])
        self.assertIn("192.168.0.0/16", on)
        self.assertNotIn("192.168.0.0/16", off)

    def test_remember_keeps_recent_addresses_only(self):
        now = time.time()
        state = {"seen": {"1.1.1.1": now - 60, "2.2.2.2": now - 10 * 3600}}
        addresses = hg.remember(state, {"3.3.3.3": "host"}, keep_hours=6)
        self.assertEqual(sorted(addresses), ["1.1.1.1", "3.3.3.3"])
        self.assertNotIn("2.2.2.2", state["seen"])


class RenderTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(directory, ignore_errors=True))
        self.dir = Path(directory)

    def test_nftables_install_script(self):
        text = firewall.NftBackend(self.dir).render(NETS)
        self.assertIn("policy drop;", text)
        self.assertIn("add element inet hotspot_guard allow4 { 127.0.0.0/8, 1.2.3.4/32 }", text)
        self.assertIn("add element inet hotspot_guard allow6 { ::1/128, 2001:db8::/32 }", text)

    def test_nftables_update_script_flushes_before_adding(self):
        text = firewall.NftBackend(self.dir).render_update(NETS)
        self.assertTrue(text.startswith("flush set inet hotspot_guard allow4\n"))
        self.assertIn("add element inet hotspot_guard allow6", text)

    def test_nftables_chunks_long_lists(self):
        many = [f"10.{i // 256}.{i % 256}.0/24" for i in range(600)]
        text = firewall.NftBackend(self.dir).render(many)
        self.assertEqual(text.count("add element inet hotspot_guard allow4"), 3)

    def test_pf_anchor_drops_outbound_by_default(self):
        self.assertIn("block drop out quick all", firewall.PF_ANCHOR_RULES)
        self.assertIn("pass out quick to <hg_allowed> keep state", firewall.PF_ANCHOR_RULES)
        self.assertLess(firewall.PF_ANCHOR_RULES.index("pass out quick to <hg_allowed>"),
                        firewall.PF_ANCHOR_RULES.index("block drop out quick all"))

    def test_windows_script_blocks_outbound_and_allows_list(self):
        text = firewall.WindowsBackend(self.dir).render(NETS)
        self.assertIn("-RemoteAddress @('127.0.0.0/8','::1/128','1.2.3.4/32','2001:db8::/32')", text)
        self.assertIn("-DefaultOutboundAction Block", text)
        self.assertIn("-Group $group", text)

    def test_dry_run_render_does_not_touch_the_system(self):
        self.assertIn("policy drop;", firewall.NftBackend(self.dir).render(NETS))
        self.assertFalse(any(self.dir.iterdir()))  # render writes nothing


class NftSyntaxTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("nft"), "nft is not installed")
    def test_install_script_passes_nft_check_mode(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(directory, ignore_errors=True))
        script = Path(directory) / "check.nft"
        script.write_text(firewall.NftBackend(Path(directory)).render(NETS))
        result = subprocess.run(["nft", "-c", "-f", str(script)], capture_output=True, text=True)
        if result.returncode != 0 and "Operation not permitted" in result.stderr:
            self.skipTest("nft check mode needs CAP_NET_ADMIN here")
        self.assertEqual(result.returncode, 0, result.stderr)


def temp_file(text, name="test.ini"):
    directory = tempfile.mkdtemp()
    path = Path(directory) / name
    path.write_text(text, encoding="utf-8")
    return path


GROUP_CONFIG = (
    "# keep this comment\n"
    "[settings]\n"
    "refresh_seconds = 300\n"
    "\n"
    "[social]\n"
    "enabled = yes\n"
    "description = Social\n"
    "domains =\n"
    "    example.com\n"
    "\n"
    "[dev]\n"
    "enabled = no\n"
    "domains =\n"
    "    github.com\n"
)


class GroupEditTests(unittest.TestCase):
    def test_list_groups_reports_state_and_counts(self):
        rows = {row["name"]: row for row in hg.list_groups(temp_file(GROUP_CONFIG))}
        self.assertEqual(set(rows), {"social", "dev"})
        self.assertTrue(rows["social"]["enabled"])
        self.assertEqual(rows["social"]["domain_count"], 1)
        self.assertEqual(rows["social"]["description"], "Social")
        self.assertFalse(rows["dev"]["enabled"])
        self.assertEqual(rows["dev"]["description"], "")

    def test_set_group_changes_only_the_enabled_line(self):
        path = temp_file(GROUP_CONFIG)
        before = path.read_text().splitlines()
        hg.set_group(path, "dev", True)
        after = path.read_text().splitlines()
        self.assertEqual(len(before), len(after))
        changed = [(a, b) for a, b in zip(before, after) if a != b]
        self.assertEqual(changed, [("enabled = no", "enabled = yes")])
        self.assertIn("# keep this comment", after)
        self.assertTrue(hg.list_groups(path)[1]["enabled"])

    def test_set_group_rejects_unknown_names_and_settings(self):
        path = temp_file(GROUP_CONFIG)
        with self.assertRaises(SystemExit):
            hg.set_group(path, "nope", True)
        with self.assertRaises(SystemExit):
            hg.set_group(path, "settings", True)


class WatcherTests(unittest.TestCase):
    def test_missing_pid_file_means_not_watching(self):
        with mock.patch.multiple(hg, PID_FILE=Path("/nonexistent/watch.pid"), SYSTEM="Linux"):
            self.assertIsNone(hg.watcher_pid())

    def test_pid_of_an_unrelated_process_is_not_trusted(self):
        # A live pid that is not a hotspot-guard watch command must never be signalled.
        pid_file = temp_file(str(os.getpid()), name="watch.pid")
        with mock.patch.multiple(hg, PID_FILE=pid_file, SYSTEM="Linux"):
            self.assertIsNone(hg.watcher_pid())


class CliJsonTests(unittest.TestCase):
    def test_plan_json(self):
        path = temp_file("[x]\nenabled = yes\ndomains =\n    example.com\n    gone.example\n")
        out = io.StringIO()
        with mock.patch.object(hg, "resolve", return_value=({"1.2.3.4": "example.com"}, ["gone.example"])), \
                redirect_stdout(out):
            hg.main(["--config", str(path), "plan", "--json"])
        data = json.loads(out.getvalue())
        self.assertEqual(data["addresses"], [{"host": "example.com", "ip": "1.2.3.4"}])
        self.assertEqual(data["failed"], ["gone.example"])
        self.assertGreater(data["networks"], 0)

    def test_groups_json_and_set_group_round_trip(self):
        path = temp_file(GROUP_CONFIG)
        out = io.StringIO()
        with redirect_stdout(out):
            hg.main(["--config", str(path), "groups", "--json"])
        self.assertEqual([row["name"] for row in json.loads(out.getvalue())], ["social", "dev"])
        with redirect_stdout(io.StringIO()):
            hg.main(["--config", str(path), "set-group", "dev", "on"])
        self.assertTrue(hg.list_groups(path)[1]["enabled"])

    def test_status_json_reads_recorded_state_only(self):
        state_file = temp_file(json.dumps({"active": True, "applied_at": 1.0, "seen": {"1.1.1.1": 1.0}}),
                               name="state.json")
        out = io.StringIO()
        with mock.patch.multiple(hg, STATE_FILE=state_file, PID_FILE=Path("/nonexistent/watch.pid"), SYSTEM="Linux"), \
                redirect_stdout(out):
            hg.main(["status", "--json"])
        data = json.loads(out.getvalue())
        self.assertTrue(data["active"])
        self.assertFalse(data["watching"])
        self.assertEqual(data["remembered"], 1)


if __name__ == "__main__":
    unittest.main()
