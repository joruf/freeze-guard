"""Unit tests for the traffic light in run-monitor.py.

These thresholds are the whole product: they decide when the guard kills, when
the dashboard shouts and when the window pulls itself to the front. Every branch
gets a test, and the order between branches is tested too - the earliest signal
that still leaves time to act has to win.
"""

from __future__ import annotations

import unittest

from tests.fg_test_utils import load_monitor

M = load_monitor()


def sample(**overrides):
    """A calm sample with only the fields alert_level() reads."""
    base = {
        "mem_full60": 0.0,
        "mem_full10": 0.0,
        "mem_some10": 0.0,
        "mem_avail_pct": 55.0,
        "swapout_kbs": 0.0,
        "majflt_s": 0.0,
        "kmsg_new": 0,
        "runaways": [],
    }
    base.update(overrides)
    return base


class TestAlertLevel(unittest.TestCase):
    """Verifies each threshold and the priority between them."""

    def level(self, **overrides):
        return M.alert_level(sample(**overrides))

    def test_calm_sample_is_green(self) -> None:
        level, text, (key, args) = self.level()
        self.assertEqual(level, M.GREEN)
        self.assertEqual(key, "reason.calm")
        self.assertEqual(args, {})
        self.assertTrue(text)

    def test_guard_kill_condition_is_red(self) -> None:
        """PSI full60 >= 20 % together with < 15 % available is the daemon's own
        kill condition, so the dashboard must be red before it fires."""
        level, _text, (key, _a) = self.level(mem_full60=20.0, mem_avail_pct=14.9)
        self.assertEqual(level, M.RED)
        self.assertEqual(key, "reason.stall")

    def test_kill_condition_needs_both_halves(self) -> None:
        self.assertNotEqual(self.level(mem_full60=25.0, mem_avail_pct=30.0)[0], M.RED)
        self.assertNotEqual(self.level(mem_full60=5.0, mem_avail_pct=10.0)[2][0],
                            "reason.stall")

    def test_total_blockage_is_red(self) -> None:
        level, _t, (key, _a) = self.level(mem_full10=30.0)
        self.assertEqual(level, M.RED)
        self.assertEqual(key, "reason.all_blocked")

    def test_new_kernel_stalls_are_red(self) -> None:
        level, _t, (key, _a) = self.level(kmsg_new=1)
        self.assertEqual(level, M.RED)
        self.assertEqual(key, "reason.kernel_stalls")

    def test_full_blockage_started_is_orange(self) -> None:
        level, _t, (key, _a) = self.level(mem_full10=5.0)
        self.assertEqual(level, M.ORANGE)
        self.assertEqual(key, "reason.full_started")

    def test_below_twelve_percent_is_orange(self) -> None:
        level, _t, (key, _a) = self.level(mem_avail_pct=11.9)
        self.assertEqual(level, M.ORANGE)
        self.assertEqual(key, "reason.below12")

    def test_heavy_swapping_is_orange(self) -> None:
        level, _t, (key, _a) = self.level(swapout_kbs=5001)
        self.assertEqual(level, M.ORANGE)
        self.assertEqual(key, "reason.thrashing")

    def test_waiting_tasks_are_yellow(self) -> None:
        level, _t, (key, _a) = self.level(mem_some10=10.0)
        self.assertEqual(level, M.YELLOW)
        self.assertEqual(key, "reason.waiting")

    def test_tight_memory_is_yellow(self) -> None:
        level, _t, (key, _a) = self.level(mem_avail_pct=24.9)
        self.assertEqual(level, M.YELLOW)
        self.assertEqual(key, "reason.tight")

    def test_any_swapping_out_is_yellow(self) -> None:
        level, _t, (key, _a) = self.level(swapout_kbs=1)
        self.assertEqual(level, M.YELLOW)
        self.assertEqual(key, "reason.swapping")

    def test_major_faults_alone_are_yellow(self) -> None:
        level, _t, (key, _a) = self.level(majflt_s=51)
        self.assertEqual(level, M.YELLOW)
        self.assertEqual(key, "reason.swapping")

    def test_cpu_runaway_is_yellow_and_names_the_process(self) -> None:
        """A busy loop alone does not freeze Linux, so it warns rather than
        escalates - but it must name the suspect."""
        level, text, (key, args) = self.level(runaways=[(1024, 97.0, "node", "4711")])
        self.assertEqual(level, M.YELLOW)
        self.assertEqual(key, "reason.runaway")
        self.assertEqual(args["comm"], "node")
        self.assertEqual(args["pct"], "97")
        self.assertIn("node", text)

    def test_red_beats_orange_and_yellow(self) -> None:
        level, _t, (key, _a) = self.level(mem_full10=45.0, mem_avail_pct=9.0,
                                          swapout_kbs=9000, mem_some10=50.0)
        self.assertEqual(level, M.RED)
        self.assertEqual(key, "reason.all_blocked")

    def test_orange_beats_yellow(self) -> None:
        level, _t, (key, _a) = self.level(mem_full10=6.0, mem_avail_pct=20.0,
                                          swapout_kbs=10)
        self.assertEqual(level, M.ORANGE)
        self.assertEqual(key, "reason.full_started")

    def test_levels_are_ordered(self) -> None:
        """The GUI compares levels numerically for its auto-raise threshold."""
        self.assertLess(M.GREEN, M.YELLOW)
        self.assertLess(M.YELLOW, M.ORANGE)
        self.assertLess(M.ORANGE, M.RED)

    def test_every_branch_ships_a_translatable_key(self) -> None:
        cases = [
            {}, {"mem_full60": 25.0, "mem_avail_pct": 10.0}, {"mem_full10": 40.0},
            {"kmsg_new": 3}, {"mem_full10": 6.0}, {"mem_avail_pct": 11.0},
            {"swapout_kbs": 9000}, {"mem_some10": 15.0}, {"mem_avail_pct": 20.0},
            {"swapout_kbs": 5}, {"runaways": [(1, 99.0, "x", "2")]},
        ]
        for case in cases:
            with self.subTest(case=case):
                _level, text, (key, args) = self.level(**case)
                self.assertTrue(key.startswith("reason."), key)
                self.assertIsInstance(args, dict)
                self.assertTrue(text.strip())


class TestLevelNames(unittest.TestCase):
    """The recorder writes these names into the CSV, so they are a data format."""

    def test_csv_level_names_are_ascii(self) -> None:
        for name in M.LEVEL_NAME.values():
            self.assertEqual(name, name.encode("ascii", "replace").decode())

    def test_all_four_levels_have_a_name(self) -> None:
        self.assertEqual(set(M.LEVEL_NAME), {M.GREEN, M.YELLOW, M.ORANGE, M.RED})


if __name__ == "__main__":
    unittest.main()
