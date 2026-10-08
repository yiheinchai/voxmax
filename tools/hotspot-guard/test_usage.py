"""Tests for data-usage tracking. Run from this directory: python3 -m unittest -v test_usage"""

import ipaddress
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import usage

ROUTE = (
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
    "eth0\t0000000A\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0\n"
    "wlan0\t00000000\t0102A8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0\n"
)
PROC_NET_DEV = (
    "Inter-|   Receive                                                |  Transmit\n"
    " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n"
    "    lo:  143957      14    0    0    0     0          0         0   143957      14    0    0    0     0       0          0\n"
    " wlan0: 9000000   6000    0    0    0     0          0         0  2000000    4000    0    0    0     0       0          0\n"
)
CONNTRACK = (
    "ipv4     2 tcp      6 431999 ESTABLISHED src=10.77.0.2 dst=203.0.113.10 sport=45678 dport=8080 "
    "packets=10 bytes=1200 src=203.0.113.10 dst=10.77.0.2 sport=8080 dport=45678 packets=8 bytes=5000 "
    "[ASSURED] mark=0 use=2\n"
    "ipv6     10 tcp      6 431999 ESTABLISHED src=fd77::2 dst=64:ff9b::cb00:710a sport=50000 dport=443 "
    "packets=4 bytes=300 src=64:ff9b::cb00:710a dst=fd77::2 sport=443 dport=50000 packets=3 bytes=900 "
    "[ASSURED] mark=0 use=2\n"
)
PF_STATES = (
    "all tcp 192.168.1.10:53144 -> 17.253.144.10:443       ESTABLISHED:ESTABLISHED\n"
    "   [ Creation: 00:00:06 ]\n"
    "   [ Bytes: In: 3893 Out: 8523 ]\n"
    "all udp 192.168.1.10:53 -> 192.168.1.1:53       SINGLE:MULTIPLE\n"
    "   [ Bytes: In: 120 Out: 80 ]\n"
)


class ParserTests(unittest.TestCase):
    def test_default_route_interface(self):
        self.assertEqual(usage.parse_route(ROUTE), "wlan0")

    def test_no_default_route(self):
        self.assertIsNone(usage.parse_route("Iface\tDestination\neth0\t0000000A\n"))

    def test_proc_net_dev_counters(self):
        self.assertEqual(usage.parse_proc_net_dev(PROC_NET_DEV, "wlan0"), (9000000, 2000000))
        self.assertIsNone(usage.parse_proc_net_dev(PROC_NET_DEV, "nope0"))

    def test_macos_netstat_row(self):
        text = (
            "Name  Mtu   Network       Address            Ipkts Ierrs     Ibytes    Opkts Oerrs     Obytes  Coll\n"
            "en0   1500  <Link#6>      aa:bb:cc:dd:ee:ff  123456     0  987654321   65432     0   12345678     0\n"
            "en0   1500  192.168.1.0   192.168.1.5        123456     0  987654321   65432     0   12345678     0\n"
        )
        self.assertEqual(usage.parse_netstat_ibi(text, "en0"), (987654321, 12345678))

    def test_windows_netstat_totals(self):
        text = "Interface Statistics\n\n                           Received            Sent\n\nBytes                       9000000          2000000\n"
        self.assertEqual(usage.parse_netstat_e(text), (9000000, 2000000))

    def test_conntrack_connections_are_keyed_with_their_remote_address(self):
        counters = usage.parse_conntrack(CONNTRACK)
        self.assertEqual(counters["tcp 10.77.0.2:45678 203.0.113.10:8080"], ("203.0.113.10", 6200))
        self.assertEqual(counters["tcp fd77::2:50000 64:ff9b::cb00:710a:443"][1], 1200)

    def test_conntrack_without_accounting_is_unavailable(self):
        self.assertIsNone(usage.parse_conntrack(CONNTRACK.replace("bytes=", "x=")))

    def test_pf_states_with_bytes(self):
        counters = usage.parse_pf_states(PF_STATES)
        self.assertEqual(counters["tcp 192.168.1.10:53144 17.253.144.10:443"], ("17.253.144.10", 12416))
        self.assertEqual(counters["udp 192.168.1.10:53 192.168.1.1:53"], ("192.168.1.1", 200))


class AttributionTests(unittest.TestCase):
    def test_social_destinations_are_separated(self):
        connections = {"a": ("203.0.113.10", 700), "b": ("198.51.100.7", 300)}
        social, other = usage.attribute(connections, {}, ["203.0.113.10/32"])
        self.assertEqual((social, other), (700, 300))

    def test_nat64_destinations_map_back_to_the_ipv4_site(self):
        connections = {"a": ("64:ff9b::cb00:710a", 700), "b": ("198.51.100.7", 300)}
        social, other = usage.attribute(connections, {}, ["203.0.113.10/32"], ipaddress.IPv6Network("64:ff9b::/96"))
        self.assertEqual((social, other), (700, 300))

    def test_only_new_bytes_count(self):
        connections = {"a": ("203.0.113.10", 1000)}
        social, _ = usage.attribute(connections, {"a": 400}, ["203.0.113.10/32"])
        self.assertEqual(social, 600)

    def test_a_reset_counter_counts_everything_it_shows(self):
        connections = {"a": ("203.0.113.10", 150)}
        social, _ = usage.attribute(connections, {"a": 400}, ["203.0.113.10/32"])
        self.assertEqual(social, 150)


class SamplingTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)

    def sample(self, iface, counters, connections, social=("203.0.113.10/32",), now=None):
        with mock.patch.object(usage, "default_interface", return_value=iface), \
                mock.patch.object(usage, "interface_bytes", return_value=counters), \
                mock.patch.object(usage, "connection_counters", return_value=connections):
            return usage.take_sample(self.dir, list(social), None, now=now)

    def test_first_sample_only_sets_the_baseline(self):
        self.assertIsNone(self.sample("wlan0", (1000, 100), {}))
        self.assertTrue((self.dir / usage.BASELINE).exists())

    def test_later_sample_records_the_change_with_a_split(self):
        self.sample("wlan0", (1000, 100), {"a": ("203.0.113.10", 500)})
        row = self.sample("wlan0", (3000, 300), {"a": ("203.0.113.10", 900),
                                                  "b": ("198.51.100.7", 400)}, now=1_700_000_000)
        self.assertEqual(row["total"], 2200)
        self.assertEqual(row["rx"], 2000)
        self.assertEqual(row["social"] + row["other"], 2200)
        self.assertGreater(row["social"], 0)
        self.assertGreater(row["other"], 0)

    def test_a_changed_interface_starts_a_new_baseline(self):
        self.sample("wlan0", (1000, 100), {})
        self.assertIsNone(self.sample("eth0", (5000, 500), {}))

    def test_no_per_connection_counters_means_no_split(self):
        self.sample("wlan0", (1000, 100), None)
        row = self.sample("wlan0", (2000, 200), None)
        self.assertEqual(row["total"], 1100)
        self.assertIsNone(row["social"])
        self.assertEqual(row["method"], "none")

    def test_counter_reset_does_not_produce_negative_usage(self):
        self.sample("wlan0", (9000, 900), {})
        row = self.sample("wlan0", (100, 10), {})
        self.assertEqual(row["total"], 0)

    def test_history_survives_a_new_process(self):
        self.sample("wlan0", (1000, 100), {})
        self.sample("wlan0", (2000, 200), {}, now=time.time())
        rows = usage.read_rows(self.dir, since=0)
        self.assertEqual(len(rows), 1)


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.now = 1_700_000_000

    def write(self, rows):
        with (self.dir / usage.HISTORY).open("w") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")

    def test_buckets_and_totals_with_a_split(self):
        self.write([
            {"t": self.now - 60, "total": 100, "social": 60, "other": 40, "method": "conntrack"},
            {"t": self.now - 30, "total": 50, "social": 10, "other": 40, "method": "conntrack"},
        ])
        summary = usage.summarize(self.dir, hours=1, bucket_seconds=3600, now=self.now)
        self.assertEqual(summary["totals"], {"all": 150, "social": 70, "other": 80, "split_coverage": 1.0})
        self.assertEqual(summary["methods"], ["conntrack"])
        self.assertEqual(len(summary["buckets"]), 1)

    def test_rows_without_a_split_are_counted_but_not_split(self):
        self.write([
            {"t": self.now - 60, "total": 100, "social": None, "other": None, "method": "none"},
            {"t": self.now - 30, "total": 100, "social": 50, "other": 50, "method": "conntrack"},
        ])
        summary = usage.summarize(self.dir, hours=1, bucket_seconds=3600, now=self.now)
        self.assertEqual(summary["totals"]["all"], 200)
        self.assertEqual(summary["totals"]["split_coverage"], 0.5)

    def test_no_split_at_all(self):
        self.write([{"t": self.now - 60, "total": 100, "social": None, "other": None, "method": "none"}])
        summary = usage.summarize(self.dir, hours=1, now=self.now)
        self.assertIsNone(summary["totals"]["social"])
        self.assertIsNone(summary["totals"]["other"])

    def test_rows_outside_the_period_are_left_out(self):
        self.write([{"t": self.now - 10 * 3600, "total": 999, "social": 0, "other": 999, "method": "pf"}])
        summary = usage.summarize(self.dir, hours=2, now=self.now)
        self.assertEqual(summary["totals"]["all"], 0)
        self.assertIsNone(summary["totals"]["split_coverage"])

    def test_pruning_drops_rows_older_than_the_retention(self):
        path = self.dir / usage.HISTORY
        self.write([
            {"t": self.now - usage.RETENTION_SECONDS - 10, "total": 1, "social": 0, "other": 1, "method": "pf"},
            {"t": self.now - 10, "total": 2, "social": 0, "other": 2, "method": "pf"},
        ])
        usage._prune(path, now=self.now)
        self.assertEqual([row["total"] for row in usage.read_rows(self.dir, since=0)], [2])


if __name__ == "__main__":
    unittest.main()
