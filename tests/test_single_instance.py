"""Tests for the single-instance guard.

Same mechanism as the other tools in ~/Applications: an exclusive flock plus a
control socket. Two instances here would sample /proc twice, register the same
tray icon twice and report one incident twice, so this is worth testing on the
real primitives rather than with mocks - including the two cases that matter in
practice: a crashed owner (stale lock, stale socket) and an owner that holds the
lock but no longer answers.
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import fg_instance
from tests.fg_test_utils import GUI_PATH, ROOT, has_gtk


class TestLockFile(unittest.TestCase):
    """flock semantics, the pid inside, and release."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.lock = Path(self.tmp.name) / "instance.lock"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def guard(self):
        return fg_instance.SingleInstanceGuard(self.lock)

    def test_first_acquire_wins(self) -> None:
        guard = self.guard()
        self.assertTrue(guard.acquire())
        guard.release()

    def test_second_acquire_is_refused(self) -> None:
        first = self.guard()
        self.assertTrue(first.acquire())
        try:
            self.assertFalse(self.guard().acquire())
        finally:
            first.release()

    def test_after_release_the_next_one_may_start(self) -> None:
        first = self.guard()
        first.acquire()
        first.release()
        second = self.guard()
        self.assertTrue(second.acquire())
        second.release()

    def test_release_is_idempotent(self) -> None:
        guard = self.guard()
        guard.acquire()
        guard.release()
        guard.release()

    def test_the_lock_names_its_owner(self) -> None:
        guard = self.guard()
        guard.acquire()
        try:
            self.assertEqual(guard.owner_pid(), os.getpid())
        finally:
            guard.release()

    def test_missing_lock_file_has_no_owner(self) -> None:
        self.assertIsNone(self.guard().owner_pid())

    def test_garbage_in_the_lock_file_is_not_an_owner(self) -> None:
        self.lock.write_text("not a pid")
        self.assertIsNone(self.guard().owner_pid())

    def test_the_directory_is_created(self) -> None:
        nested = Path(self.tmp.name) / "sub" / "dir" / "instance.lock"
        guard = fg_instance.SingleInstanceGuard(nested)
        self.assertTrue(guard.acquire())
        guard.release()
        self.assertTrue(nested.exists())

    def test_a_dead_owner_does_not_block_the_next_start(self) -> None:
        """The case that makes a plain pid file useless: after a crash nobody
        releases anything, and the kernel has to do it."""
        code = (f"import fcntl,time;h=open({str(self.lock)!r},'w');"
                "fcntl.flock(h.fileno(), fcntl.LOCK_EX);h.write('x');h.flush();"
                "print('locked',flush=True);time.sleep(60)")
        child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                                 text=True)
        try:
            child.stdout.readline()
            self.assertFalse(self.guard().acquire(), "the child holds the lock")
            child.send_signal(signal.SIGKILL)
            child.wait(timeout=10)
            guard = self.guard()
            self.assertTrue(guard.acquire(), "a killed owner must release the lock")
            guard.release()
        finally:
            if child.poll() is None:
                child.kill()

    def test_an_unwritable_runtime_directory_does_not_stop_the_program(self) -> None:
        """Refusing to start because a lock cannot be written would be worse than
        the duplicate it prevents."""
        guard = fg_instance.SingleInstanceGuard("/proc/nope/instance.lock")
        self.assertTrue(guard.acquire())
        guard.release()

    def test_paths_follow_the_house_convention(self) -> None:
        self.assertEqual(fg_instance.LOCK_DIR.name, "freeze-guard")
        self.assertEqual(fg_instance.LOCK_FILE.name, "instance.lock")
        self.assertEqual(fg_instance.SOCKET_FILE.name, "control.sock")
        self.assertIn(str(fg_instance.RUNTIME_DIR), str(fg_instance.LOCK_DIR))


class TestControlSocket(unittest.TestCase):
    """The channel a second start uses to raise the first window."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.sock = Path(self.tmp.name) / "control.sock"
        self.shows = []
        self.server = None

    def tearDown(self) -> None:
        if self.server is not None:
            self.server.stop()
        self.tmp.cleanup()

    def serve(self):
        """Waits until the path really is a bound socket.

        Not merely until it exists: in the stale-file case a regular file is
        already sitting there, and connecting to that fails.
        """
        self.server = fg_instance.InstanceControlServer(
            lambda: self.shows.append(1), self.sock)
        self.server.start()
        for _ in range(100):
            try:
                if stat.S_ISSOCK(self.sock.stat().st_mode):
                    return self.server
            except OSError:
                pass
            time.sleep(0.05)
        self.fail("the control socket never appeared")

    def wait_for_shows(self, count, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline and len(self.shows) < count:
            time.sleep(0.02)
        return len(self.shows)

    def test_show_request_reaches_the_running_instance(self) -> None:
        self.serve()
        self.assertTrue(fg_instance.request_show_existing_instance(self.sock))
        self.assertEqual(self.wait_for_shows(1), 1)

    def test_several_requests_all_arrive(self) -> None:
        self.serve()
        for _ in range(3):
            self.assertTrue(fg_instance.request_show_existing_instance(self.sock))
        self.assertEqual(self.wait_for_shows(3), 3)

    def test_without_a_server_the_request_fails_quietly(self) -> None:
        self.assertFalse(fg_instance.request_show_existing_instance(self.sock))

    def test_a_stale_socket_file_is_replaced_not_refused(self) -> None:
        """A crashed instance leaves the file behind; bind() would fail on it."""
        self.sock.write_text("")
        self.serve()
        self.assertTrue(fg_instance.request_show_existing_instance(self.sock))
        self.assertEqual(self.wait_for_shows(1), 1)

    def test_a_stale_socket_file_alone_does_not_pretend_to_answer(self) -> None:
        self.sock.write_text("")
        self.assertFalse(fg_instance.request_show_existing_instance(self.sock))

    def test_stop_removes_the_socket(self) -> None:
        self.serve()
        self.server.stop()
        self.server = None
        self.assertFalse(self.sock.exists())

    def test_garbage_does_not_trigger_a_show(self) -> None:
        import socket as socketlib

        self.serve()
        client = socketlib.socket(socketlib.AF_UNIX, socketlib.SOCK_STREAM)
        client.connect(str(self.sock))
        client.sendall(b"HELLO")
        client.close()
        time.sleep(0.3)
        self.assertEqual(self.shows, [])


class TestEnforce(unittest.TestCase):
    """The decision a start makes, including the quiet path for the autostart."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = (fg_instance.LOCK_FILE, fg_instance.SOCKET_FILE)
        fg_instance.LOCK_FILE = Path(self.tmp.name) / "instance.lock"
        fg_instance.SOCKET_FILE = Path(self.tmp.name) / "control.sock"

    def tearDown(self) -> None:
        fg_instance.LOCK_FILE, fg_instance.SOCKET_FILE = self._saved
        self.tmp.cleanup()

    def test_a_free_lock_lets_the_program_continue(self) -> None:
        may_continue, guard = fg_instance.enforce_single_instance()
        self.assertTrue(may_continue)
        guard.release()

    def test_a_taken_lock_blocks_and_reports(self) -> None:
        first = fg_instance.SingleInstanceGuard()
        first.acquire()
        seen = []
        try:
            may_continue, _guard = fg_instance.enforce_single_instance(
                on_blocked=lambda focused, pid: seen.append((focused, pid)))
            self.assertFalse(may_continue)
            self.assertEqual(len(seen), 1)
            focused, pid = seen[0]
            self.assertFalse(focused, "no control server is running in this test")
            self.assertEqual(pid, os.getpid())
        finally:
            first.release()

    def test_quiet_blocks_without_saying_anything(self) -> None:
        """The autostart entry starts with --quiet: a dialog at login, before the
        user has done anything, would be noise."""
        first = fg_instance.SingleInstanceGuard()
        first.acquire()
        seen = []
        try:
            may_continue, _guard = fg_instance.enforce_single_instance(
                quiet=True, on_blocked=lambda *a: seen.append(a))
            self.assertFalse(may_continue)
            self.assertEqual(seen, [])
        finally:
            first.release()

    def test_a_reachable_instance_is_focused_before_blocking(self) -> None:
        first = fg_instance.SingleInstanceGuard()
        first.acquire()
        shows = []
        server = fg_instance.InstanceControlServer(lambda: shows.append(1))
        server.start()
        seen = []
        try:
            for _ in range(50):
                if fg_instance.SOCKET_FILE.exists():
                    break
                time.sleep(0.05)
            may_continue, _guard = fg_instance.enforce_single_instance(
                on_blocked=lambda focused, pid: seen.append(focused))
            self.assertFalse(may_continue)
            self.assertEqual(seen, [True])
        finally:
            server.stop()
            first.release()


@unittest.skipUnless(has_gtk(), "run.py needs PyGObject")
class TestSecondStartOfTheRealProgram(unittest.TestCase):
    """End to end: two processes, one window."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = dict(os.environ)
        # a private runtime directory, so a dashboard the user has open is not
        # disturbed by the test
        self.env["XDG_RUNTIME_DIR"] = self.tmp.name
        self.env["XDG_CONFIG_HOME"] = os.path.join(self.tmp.name, "config")
        self.first = None

    def tearDown(self) -> None:
        if self.first and self.first.poll() is None:
            self.first.terminate()
            try:
                self.first.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.first.kill()
        self.tmp.cleanup()

    def start(self, *args):
        return subprocess.Popen(
            [sys.executable, str(GUI_PATH), *args], env=self.env, cwd=ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True)

    def lock_path(self):
        return Path(self.tmp.name) / "freeze-guard" / "instance.lock"

    def test_the_lock_is_taken_and_names_the_running_process(self) -> None:
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            self.skipTest("needs a display")
        self.first = self.start("--window")
        for _ in range(60):
            if self.lock_path().exists() and self.lock_path().read_text().strip():
                break
            time.sleep(0.25)
        self.assertEqual(self.lock_path().read_text().strip(), str(self.first.pid))

    def test_a_second_start_exits_one_and_explains_itself(self) -> None:
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            self.skipTest("needs a display")
        self.first = self.start("--window")
        for _ in range(60):
            if (Path(self.tmp.name) / "freeze-guard" / "control.sock").exists():
                break
            time.sleep(0.25)
        second = self.start("--window", "--quiet")
        stdout, stderr = second.communicate(timeout=60)
        self.assertEqual(second.returncode, 1, stdout + stderr)
        self.assertIsNone(self.first.poll(), "the first instance must keep running")

    def test_quiet_second_start_stays_silent(self) -> None:
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            self.skipTest("needs a display")
        self.first = self.start("--window")
        for _ in range(60):
            if self.lock_path().exists():
                break
            time.sleep(0.25)
        second = self.start("--quiet")
        stdout, stderr = second.communicate(timeout=60)
        self.assertEqual(second.returncode, 1)
        self.assertEqual(stdout.strip(), "")
        self.assertNotIn("already running", stderr.lower())

    def test_the_autostart_entry_uses_the_quiet_flag(self) -> None:
        """At login the tray icon may already be there; a dialog then is noise."""
        self.assertIn("--quiet", open(GUI_PATH).read().split("Exec={exe}")[1][:40])


if __name__ == "__main__":
    unittest.main()
