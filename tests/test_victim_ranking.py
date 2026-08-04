"""Unit tests for the victim ranking in run.py.

This mirrors select_victim() in fg-daemon.sh. If the two ever disagree, the
Simulation page lies about what the daemon will do - which is worse than showing
nothing at all. The last test therefore compares both rule sets directly.
"""

from __future__ import annotations

import re
import subprocess
import unittest

from tests.fg_test_utils import DAEMON_PATH, has_gtk, load_gui

if has_gtk():
    G = load_gui()

MB = 1024  # RSS is handed around in kB


def proc(rss_mb, comm, pid="1000", cpu=0.0):
    """One row in the shape scan_processes() produces: (rss_kb, cpu, comm, pid)."""
    return (rss_mb * MB, cpu, comm, pid)


@unittest.skipUnless(has_gtk(), "run.py needs PyGObject")
class TestVictimRanking(unittest.TestCase):
    """Weights, the 200 MB floor and the suspect bonus."""

    def test_size_dominates_inside_one_tier(self) -> None:
        rows, target = G.rank_victims(
            [proc(900, "mariadbd", "1001"), proc(400, "joplin", "1002")], set())
        self.assertEqual(target[1], "mariadbd")
        self.assertEqual([r[1] for r in rows], ["mariadbd", "joplin"])

    def test_preferred_processes_count_triple(self) -> None:
        """A 400 MB node is safe to lose, a 900 MB database is not - so the build
        helper goes first even though it is smaller."""
        _rows, target = G.rank_victims(
            [proc(900, "mariadbd", "1001"), proc(400, "node", "1002")], set())
        self.assertEqual(target[1], "node")

    def test_ide_is_only_taken_when_it_dwarfs_everything(self) -> None:
        _rows, target = G.rank_victims(
            [proc(2000, "code", "1001"), proc(400, "joplin", "1002")], set())
        self.assertEqual(target[1], "joplin")
        _rows, target = G.rank_victims(
            [proc(9000, "code", "1001"), proc(400, "joplin", "1002")], set())
        self.assertEqual(target[1], "code")

    def test_system_processes_are_never_targets(self) -> None:
        for comm in ("systemd", "Xorg", "cinnamon", "sshd", "dbus-daemon",
                     "pipewire", "earlyoom", "freeze-guard", "fg-monitor", "fg-gui"):
            with self.subTest(comm=comm):
                rows, target = G.rank_victims([proc(4000, comm, "7")], set())
                self.assertIsNone(target)
                self.assertEqual(rows[0][4], 0.0)  # weight
                self.assertEqual(rows[0][6], G.t("sim.tier_protected"))

    def test_nothing_under_two_hundred_megabytes(self) -> None:
        _rows, target = G.rank_victims([proc(199, "node", "1001")], set())
        self.assertIsNone(target)
        _rows, target = G.rank_victims([proc(200, "node", "1001")], set())
        self.assertEqual(target[1], "node")

    def test_sustained_load_doubles_the_score(self) -> None:
        rows, target = G.rank_victims(
            [proc(600, "python3", "1001", cpu=99.0), proc(900, "joplin", "1002")], {"1001"})
        self.assertEqual(target[1], "python3")
        suspect = next(r for r in rows if r[0] == "1001")
        self.assertEqual(suspect[4], 2.0)
        self.assertIn(G.t("sim.tier_suspect").strip(" ·"), suspect[6])

    def test_suspect_bonus_never_resurrects_a_protected_process(self) -> None:
        rows, target = G.rank_victims([proc(4000, "Xorg", "1009", cpu=99.0)], {"1009"})
        self.assertIsNone(target)
        self.assertEqual(rows[0][4], 0.0)

    def test_kernel_threads_and_empty_processes_are_ignored(self) -> None:
        """pid 1 and 2 are init and kthreadd; a process without RSS is a ghost."""
        rows, target = G.rank_victims(
            [proc(4000, "kthreadd", "2"), (0, 0.0, "ghost", "555")], set())
        self.assertIsNone(target)
        self.assertEqual(rows, [])

    def test_rows_are_sorted_by_score(self) -> None:
        rows, _target = G.rank_victims(
            [proc(300, "joplin", "1001"), proc(300, "node", "1002"), proc(800, "code", "1003")],
            set())
        scores = [r[5] for r in rows]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_browser_renderers_with_spaces_are_matched(self) -> None:
        """comm can contain spaces ("Web Content"), which is exactly where a
        naive split() would break the tier lookup."""
        _rows, target = G.rank_victims(
            [proc(500, "Web Content", "1001"), proc(900, "joplin", "1002")], set())
        self.assertEqual(target[1], "Web Content")


@unittest.skipUnless(has_gtk(), "run.py needs PyGObject")
class TestRulesMatchTheDaemon(unittest.TestCase):
    """The GUI's prediction is only worth something if it uses the daemon's rules."""

    @staticmethod
    def _daemon_regex(name):
        source = open(DAEMON_PATH).read()
        match = re.search(rf"^{name}='([^']+)'", source, re.M)
        assert match, f"{name} not found in fg-daemon.sh"
        return match.group(1)

    def test_whitelist_prefer_and_protected_are_identical(self) -> None:
        for name, compiled in (("WHITELIST", G.WHITELIST), ("PREFER", G.PREFER),
                               ("PROTECTED", G.PROTECTED)):
            with self.subTest(name=name):
                self.assertEqual(self._daemon_regex(name), compiled.pattern)

    def test_minimum_rss_is_identical(self) -> None:
        source = open(DAEMON_PATH).read()
        match = re.search(r"^MIN_RSS_KB=(\d+)", source, re.M)
        self.assertEqual(int(match.group(1)), G.MIN_RSS_KB)

    def test_weights_are_identical(self) -> None:
        source = open(DAEMON_PATH).read()
        self.assertIn("w = 3.0", source)
        self.assertIn("w = 0.15", source)
        self.assertIn("w = w * 2.0", source)

    def test_daemon_script_is_valid_bash(self) -> None:
        result = subprocess.run(["bash", "-n", DAEMON_PATH], capture_output=True,
                                text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
