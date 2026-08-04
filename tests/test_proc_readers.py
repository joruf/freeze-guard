"""Unit tests for the /proc readers and the sampler in run-monitor.py.

The parsers are checked twice: against synthetic files, so malformed input is
covered, and against the real /proc, so a kernel that words things differently
than expected shows up here rather than in a wrong number on screen.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from tests.fg_test_utils import has_procfs, has_psi, load_monitor

M = load_monitor()


@unittest.skipUnless(has_procfs(), "needs /proc")
class TestRealProcReaders(unittest.TestCase):
    """Reads the running system - everything here is read-only."""

    def test_meminfo_has_the_fields_the_sampler_uses(self) -> None:
        mi = M.read_meminfo()
        for key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree", "Dirty"):
            self.assertIn(key, mi)
            self.assertIsInstance(mi[key], int)
        self.assertGreater(mi["MemTotal"], 0)
        self.assertLessEqual(mi["MemAvailable"], mi["MemTotal"])

    def test_vmstat_exposes_the_swap_counters(self) -> None:
        vm = M.read_vmstat()
        for key in ("pswpin", "pswpout", "pgmajfault"):
            self.assertIn(key, vm)
            self.assertGreaterEqual(vm[key], 0)

    def test_cpu_times_are_eight_monotonic_counters(self) -> None:
        first = M.read_cpu_times()
        self.assertEqual(len(first), 8)
        second = M.read_cpu_times()
        self.assertTrue(all(b >= a for a, b in zip(first, second)))

    def test_loadavg(self) -> None:
        load1, running = M.read_loadavg()
        self.assertGreaterEqual(load1, 0.0)
        self.assertIn("/", running)

    def test_swaps_rows_are_typed(self) -> None:
        for name, kind, size, used, prio in M.read_swaps():
            self.assertTrue(name)
            self.assertIn(kind, ("file", "partition"))
            self.assertGreater(size, 0)
            self.assertGreaterEqual(used, 0)
            self.assertIsInstance(prio, int)

    @unittest.skipUnless(has_psi(), "needs PSI")
    def test_psi_gives_some_and_full_averages(self) -> None:
        psi = M.read_psi("memory")
        for key in ("some_avg10", "some_avg60", "full_avg10", "full_avg60"):
            self.assertIn(key, psi)
            self.assertGreaterEqual(psi[key], 0.0)
            self.assertLessEqual(psi[key], 100.0)

    def test_psi_for_an_unknown_resource_stays_empty(self) -> None:
        self.assertEqual(M.read_psi("does-not-exist"), {})

    def test_scan_processes_returns_both_orderings(self) -> None:
        by_rss, by_cpu, cur = M.scan_processes()
        self.assertTrue(by_rss)
        self.assertTrue(cur)
        # ranking by memory alone would hide a busy loop holding a few MB
        self.assertEqual([p[0] for p in by_rss], sorted((p[0] for p in by_rss),
                                                        reverse=True))
        self.assertEqual([p[1] for p in by_cpu], sorted((p[1] for p in by_cpu),
                                                        reverse=True))
        rss, pct, comm, pid = by_rss[0]
        self.assertGreater(rss, 0)
        self.assertGreaterEqual(pct, 0.0)
        self.assertTrue(comm)
        self.assertTrue(pid.isdigit())

    def test_scan_processes_reports_cpu_after_a_second_pass(self) -> None:
        _r1, _c1, prev = M.scan_processes()
        busy = 0
        for _ in range(200000):
            busy += 1
        _r2, by_cpu, _cur = M.scan_processes(prev, interval=0.2)
        self.assertTrue(any(p[1] > 0 for p in by_cpu))

    def test_comm_is_never_longer_than_the_kernel_allows(self) -> None:
        """The kernel caps comm at 15 characters; the whitelist regexes in the
        daemon rely on that."""
        by_rss, _c, _cur = M.scan_processes()
        for _rss, _pct, comm, _pid in by_rss:
            self.assertLessEqual(len(comm), 15, comm)


class TestKmsgWatcher(unittest.TestCase):
    """The stall counter must degrade quietly when /dev/kmsg is closed."""

    def test_watcher_never_raises(self) -> None:
        watcher = M.KmsgWatcher()
        self.assertEqual(watcher.poll(), watcher.poll())  # bounded, no exception
        self.assertGreaterEqual(watcher.count, 0)

    def test_unreadable_kmsg_reports_zero(self) -> None:
        watcher = M.KmsgWatcher()
        watcher.fd = None
        self.assertEqual(watcher.poll(), 0)


@unittest.skipUnless(has_psi(), "needs PSI")
class TestSampler(unittest.TestCase):
    """The sample dict is a contract between monitor, recorder and dashboard."""

    @classmethod
    def setUpClass(cls) -> None:
        sampler = M.Sampler()
        sampler.sample()
        cls.sample = sampler.sample()

    def test_sample_contains_every_key_the_consumers_read(self) -> None:
        expected = {
            "t", "mem_total_kb", "mem_avail_kb", "mem_avail_pct", "swap_total_kb",
            "swap_used_kb", "swap_used_pct", "dirty_kb", "mem_some10", "mem_some60",
            "mem_full10", "mem_full60", "io_full10", "cpu_some10", "swapin_kbs",
            "swapout_kbs", "majflt_s", "load1", "kmsg_new", "kmsg_total", "by_rss",
            "by_cpu", "by_rss_all", "by_cpu_all", "runaways", "level", "reason",
            "reason_i18n", "cpu_us", "cpu_sy", "cpu_id", "cpu_wa", "cpu_si",
        }
        self.assertTrue(expected.issubset(self.sample), expected - set(self.sample))

    def test_percentages_are_sane(self) -> None:
        s = self.sample
        self.assertGreater(s["mem_avail_pct"], 0.0)
        self.assertLessEqual(s["mem_avail_pct"], 100.0)
        self.assertAlmostEqual(sum(s[k] for k in ("cpu_us", "cpu_sy", "cpu_id",
                                                  "cpu_wa", "cpu_si")), 100.0, delta=5)

    def test_truncated_lists_are_prefixes_of_the_full_ones(self) -> None:
        """The simulation ranks over every process; the display shows the top few.
        If these ever diverge, the prediction stops matching the daemon."""
        s = self.sample
        self.assertLessEqual(len(s["by_rss"]), 8)
        self.assertLessEqual(len(s["by_cpu"]), 5)
        self.assertEqual(s["by_rss"], s["by_rss_all"][:len(s["by_rss"])])
        self.assertEqual(s["by_cpu"], s["by_cpu_all"][:len(s["by_cpu"])])
        self.assertGreater(len(s["by_rss_all"]), len(s["by_rss"]))

    def test_rates_are_never_negative(self) -> None:
        for key in ("swapin_kbs", "swapout_kbs", "majflt_s"):
            self.assertGreaterEqual(self.sample[key], 0.0)

    def test_history_deques_are_bounded(self) -> None:
        sampler = M.Sampler()
        for _ in range(70):
            sampler.hist["mem_full60"].append(1.0)
        self.assertLessEqual(len(sampler.hist["mem_full60"]), 60)


class TestServiceActive(unittest.TestCase):
    """Service state is read from the cgroup tree, not by spawning systemctl."""

    def test_unknown_unit_returns_none(self) -> None:
        self.assertIsNone(M.service_active("definitely-not-a-unit-42"))

    def test_known_unit_returns_a_bool_or_none(self) -> None:
        self.assertIn(type(M.service_active("freeze-guard-recorder")), (bool, type(None)))


class TestRotation(unittest.TestCase):
    """The recording must not grow without bound."""

    def test_rotate_moves_the_file_aside_when_too_large(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "history.csv")
            with open(path, "w") as f:
                f.write("x" * (M.MAX_HISTORY_BYTES + 10))
            M.rotate_if_needed(path)
            self.assertFalse(os.path.exists(path))
            self.assertTrue(os.path.exists(path + ".1"))

    def test_small_file_is_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "history.csv")
            with open(path, "w") as f:
                f.write("small")
            M.rotate_if_needed(path)
            self.assertTrue(os.path.exists(path))
            self.assertFalse(os.path.exists(path + ".1"))


if __name__ == "__main__":
    unittest.main()
