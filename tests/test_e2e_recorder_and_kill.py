"""End-to-end tests that touch the operating system.

Two flows that only mean something as a whole:

  recorder -> CSV -> forensics: the recorder runs as its own process, writes a
  real file, a freeze is simulated by cutting a hole into the timeline, and both
  readers (console and dashboard) have to find it.

  kill -> escalation: real child processes are ended the way the button does it,
  including the SIGKILL that follows a process ignoring SIGTERM.

Only processes started by these tests are ever signalled.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from tests.fg_test_utils import (MONITOR_PATH, gui_testable, has_gtk, has_psi, load_gui,
                                 load_monitor, run_monitor_cli)

M = load_monitor()
if has_gtk():
    G = load_gui()


@unittest.skipUnless(has_psi(), "the recorder needs PSI")
class TestRecorderToForensics(unittest.TestCase):
    """From a running recorder to a diagnosed outage."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "history.csv")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _record(self, seconds=3.0, interval=0.5):
        proc = subprocess.Popen(
            [sys.executable, MONITOR_PATH, "--log", "--interval", str(interval),
             "--file", self.path],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.time() + seconds
            while time.time() < deadline:
                if proc.poll() is not None:
                    self.fail(f"recorder died: {proc.stderr.read()}")
                time.sleep(0.2)
        finally:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        return proc

    def test_recorder_writes_a_readable_recording(self) -> None:
        self._record()
        with open(self.path) as f:
            lines = f.read().strip().splitlines()
        self.assertEqual(lines[0], ",".join(M.CSV_COLUMNS))
        self.assertGreaterEqual(len(lines), 3)
        for line in lines[1:]:
            fields = line.split(",", len(M.CSV_COLUMNS) - 1)
            self.assertEqual(len(fields), len(M.CSV_COLUMNS))
            float(fields[0])                              # timestamp parses
            self.assertIn(fields[1], M.LEVEL_NAME.values())
            self.assertTrue(fields[-1].startswith('"'))   # free text stays quoted

    def test_recorder_appends_instead_of_truncating(self) -> None:
        """A restart of the service must not erase the evidence of the last one."""
        self._record(seconds=2.0)
        with open(self.path) as f:
            first = len(f.read().strip().splitlines())
        self._record(seconds=2.0)
        with open(self.path) as f:
            second = len(f.read().strip().splitlines())
        self.assertGreater(second, first)
        self.assertEqual(open(self.path).read().count(",".join(M.CSV_COLUMNS)), 1)

    def test_a_hole_in_the_timeline_is_diagnosed_as_an_outage(self) -> None:
        """The freeze itself cannot be produced on purpose, but its fingerprint
        can: the recorder stops writing. That is what both readers key on."""
        self._record(seconds=3.0)
        with open(self.path) as f:
            header, *rows = f.read().strip().splitlines()
        half = len(rows) // 2
        shifted = []
        for index, row in enumerate(rows):
            fields = row.split(",", 1)
            ts = float(fields[0]) + (900 if index >= half else 0)
            shifted.append(f"{ts:.0f},{fields[1]}")
        with open(self.path, "w") as f:
            f.write("\n".join([header, *shifted]) + "\n")

        console = run_monitor_cli("--history", "--file", self.path, "--hours", "48")
        self.assertEqual(console.returncode, 0)
        self.assertIn("Ausfall", console.stdout)
        self.assertIn("15 min tot", console.stdout)

        if has_gtk():
            history = G.History(self.path, 48)
            self.assertEqual(len(history.gaps), 1)
            start, end = history.gaps[0]
            self.assertAlmostEqual(end - start, 900, delta=2)
            self.assertTrue(history.rows_before(start))

    def test_recording_survives_a_kill_without_a_broken_line(self) -> None:
        """SIGKILL is what a real freeze looks like from the file's point of view."""
        proc = subprocess.Popen(
            [sys.executable, MONITOR_PATH, "--log", "--interval", "0.4",
             "--file", self.path], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        time.sleep(2.0)
        proc.kill()
        proc.wait(timeout=10)
        history_lines = open(self.path).read().strip().splitlines()
        self.assertGreaterEqual(len(history_lines), 2)
        for line in history_lines[1:]:
            self.assertEqual(len(line.split(",", len(M.CSV_COLUMNS) - 1)),
                             len(M.CSV_COLUMNS))

    def test_rotation_keeps_the_previous_generation(self) -> None:
        with open(self.path, "w") as f:
            f.write("x" * (M.MAX_HISTORY_BYTES + 1))
        M.rotate_if_needed(self.path)
        self._record(seconds=1.5)
        self.assertTrue(os.path.exists(self.path + ".1"))
        self.assertTrue(open(self.path).read().startswith("t,level"))


@unittest.skipUnless(has_gtk(), "the kill helper lives in run.py")
class TestKillEscalation(unittest.TestCase):
    """SIGTERM now, SIGKILL after the grace period - on real processes."""

    def setUp(self) -> None:
        from gi.repository import GLib

        self.GLib = GLib
        self.messages = []
        self.original_delay = G.KILL_ESCALATE_SECONDS
        G.KILL_ESCALATE_SECONDS = 1      # the real path, without the wait
        self.children = []

    def tearDown(self) -> None:
        G.KILL_ESCALATE_SECONDS = self.original_delay
        for child in self.children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)

    def spawn(self, ignore_term=False):
        code = ("import signal,time,sys;"
                + ("signal.signal(signal.SIGTERM, signal.SIG_IGN);" if ignore_term else "")
                + "print('up', flush=True); time.sleep(120)")
        child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                                 text=True)
        child.stdout.readline()          # wait until the handler is installed
        self.children.append(child)
        return child

    def spin(self, seconds):
        """Runs a GLib main loop so the escalation timer can fire."""
        loop = self.GLib.MainLoop()
        self.GLib.timeout_add(int(seconds * 1000), lambda: (loop.quit(), False)[1])
        loop.run()

    def report(self, text):
        self.messages.append(text)

    def test_cooperative_process_dies_on_sigterm(self) -> None:
        child = self.spawn()
        G.kill_process(child.pid, "python3", self.report)
        child.wait(timeout=10)
        self.assertEqual(child.returncode, -signal.SIGTERM)
        self.assertIn(str(child.pid), self.messages[0])

    def test_stubborn_process_is_killed_after_the_grace_period(self) -> None:
        child = self.spawn(ignore_term=True)
        G.kill_process(child.pid, "python3", self.report)
        time.sleep(0.3)
        self.assertIsNone(child.poll(), "SIGTERM was ignored, as intended")
        self.spin(2.0)
        child.wait(timeout=10)
        self.assertEqual(child.returncode, -signal.SIGKILL)
        self.assertTrue(any("SIGKILL" in m for m in self.messages), self.messages)

    def test_escalation_verifies_identity_first(self) -> None:
        """If the pid was recycled in the meantime, SIGKILL must not be sent -
        that would hit an innocent process."""
        child = self.spawn(ignore_term=True)
        G.kill_process(child.pid, "definitely-not-this-process", self.report)
        self.spin(2.0)
        self.assertIsNone(child.poll(), "the comm check should have stopped SIGKILL")

    def test_already_dead_pid_is_reported_not_raised(self) -> None:
        child = self.spawn()
        child.kill()
        child.wait(timeout=5)
        G.kill_process(child.pid, "python3", self.report)
        self.assertTrue(any(str(child.pid) in m for m in self.messages))

    def test_own_process_never_needs_pkexec(self) -> None:
        """A foreign pid would go through pkexec, which must not happen for our
        own children - the test suite must not pop an authentication dialog."""
        calls = []
        real = G._pkexec_kill
        G._pkexec_kill = lambda pid, sig, report: calls.append((pid, sig))
        try:
            child = self.spawn()
            G.kill_process(child.pid, "python3", self.report)
            child.wait(timeout=10)
            self.spin(2.0)
            self.assertEqual(calls, [])
        finally:
            G._pkexec_kill = real


@unittest.skipUnless(gui_testable(), "needs GTK and a display")
class TestKillFromTheList(unittest.TestCase):
    """The path a click actually takes: row -> pid -> signal."""

    def test_stop_button_ends_the_process_in_that_row(self) -> None:
        from gi.repository import Pango

        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            killed = []
            table = G.ProcTable([("Process", 0.0, True)], 3,
                                lambda pid, comm: killed.append((pid, comm)))
            table.fill([("python3", Pango.Weight.NORMAL, child.pid, "python3")])
            iterator = table.store.get_iter_first()
            table.on_kill(table.store.get_value(iterator, table.ncols + 2),
                          table.store.get_value(iterator, table.ncols + 3))
            self.assertEqual(killed, [(child.pid, "python3")])

            G.kill_process(*killed[0], lambda _m: None)
            child.wait(timeout=10)
            self.assertEqual(child.returncode, -signal.SIGTERM)
        finally:
            if child.poll() is None:
                child.kill()


if __name__ == "__main__":
    unittest.main()
