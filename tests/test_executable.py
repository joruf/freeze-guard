"""
The single-file executable of Freeze Guard: its name, its version, its child processes.

The executable itself is never started here - ``build-exe.py`` does that after every
build. These tests pretend to be frozen and check that nothing in that mode reaches for
a Python interpreter that is not there (in the executable ``sys.executable`` is the
program itself), plus the version rules the release tag is made from.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import exe_support  # noqa: E402
import version  # noqa: E402

ENTRY = ROOT / "run.py"


def _load(name, filename):
    """Import a file of the project by path (``build-exe.py`` is not a module name)."""
    spec = importlib.util.spec_from_file_location(name, str(ROOT / filename))
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


BUILD = _load("build_exe_freeze_guard", "build-exe.py")


class FrozenTestCase(unittest.TestCase):
    """Pretend to run as the executable at ``self.executable``."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.bundle = self.tmp / "_MEIabc"
        self.bundle.mkdir()
        self.executable = self.tmp / "bin" / "freeze-guard-linux-x86_64-0.1.0-build1"
        self.executable.parent.mkdir()
        self.executable.write_bytes(b"exe")
        for patcher in (
            mock.patch.object(sys, "frozen", True, create=True),
            mock.patch.object(sys, "_MEIPASS", str(self.bundle), create=True),
            mock.patch.object(sys, "executable", str(self.executable)),
            mock.patch.object(exe_support, "IS_FROZEN", True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)


# ----------------------------------------------------------------------
# Name
# ----------------------------------------------------------------------
class ExecutableNameTests(unittest.TestCase):
    def test_the_file_is_named_after_machine_and_version(self):
        for machine, expected in (
            ("x86_64", "freeze-guard-linux-x86_64-0.3.4-build31"),
            ("AMD64", "freeze-guard-linux-x86_64-0.3.4-build31"),
            ("aarch64", "freeze-guard-linux-aarch64-0.3.4-build31"),
            ("arm64", "freeze-guard-linux-aarch64-0.3.4-build31"),
        ):
            with mock.patch.object(BUILD.platform, "machine", return_value=machine):
                self.assertEqual(BUILD.executable_name("0.3.4", "31"), expected)

    def test_the_spec_names_the_entry_script_and_the_file(self):
        text = BUILD.spec_text(Path("/stage"), "freeze-guard-linux-x86_64-0.3.4-build31")
        self.assertIn(repr(str(ENTRY)), text)
        self.assertIn("name='freeze-guard-linux-x86_64-0.3.4-build31'", text)
        self.assertIn("[('/stage', '.')]", text)

    def test_the_build_artifacts_are_ignored_by_git(self):
        lines = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
        for entry in ("build/", "dist/"):
            self.assertTrue(entry in lines or "/" + entry in lines, entry)

    def test_every_data_file_exists(self):
        for relative in BUILD.DATA_FILES:
            self.assertTrue((ROOT / relative).exists(), relative)


# ----------------------------------------------------------------------
# Version
# ----------------------------------------------------------------------
class VersionTests(unittest.TestCase):
    def test_a_stored_line_reads_back(self):
        found = version.parse("0.2.5 14 4957c55 2026-11-11")
        self.assertEqual((found.name, found.build, found.commit, found.date),
                         ("0.2.5", "14", "4957c55", "2026-11-11"))
        self.assertEqual(found.label, "0.2.5 (14), 4957c55, 2026-11-11")
        self.assertEqual(version.parse(found.line()), found)
        self.assertIsNone(version.parse("  "))

    def test_the_environment_variable_wins(self):
        with mock.patch.dict(os.environ, {version.ENVIRONMENT_VARIABLE: "1.2.3 45 abcdef0 2026-01-02"}):
            self.assertEqual(version.banner("X"), "X 1.2.3 (45), abcdef0, 2026-01-02")

    def test_the_executable_reads_the_bundled_file_and_never_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / "VERSION").write_text("0.1.9 24 1287719 2026-09-29\n", encoding="utf-8")
            environment = {k: v for k, v in os.environ.items() if k != version.ENVIRONMENT_VARIABLE}
            with mock.patch.object(version, "FROZEN", True), \
                    mock.patch.dict(os.environ, environment, clear=True), \
                    mock.patch.object(version, "_git", side_effect=AssertionError("git at run time")):
                found = version.current(root)
            self.assertEqual(found.label, "0.1.9 (24), 1287719, 2026-09-29")

    def test_no_source_says_unknown_instead_of_inventing_a_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            environment = {k: v for k, v in os.environ.items() if k != version.ENVIRONMENT_VARIABLE}
            with mock.patch.dict(os.environ, environment, clear=True):
                found = version.current(Path(tmp))
        self.assertFalse(found.is_known)
        self.assertEqual(found.label, "unknown")

    @unittest.skipIf(shutil.which("git") is None, "git is needed")
    def test_the_version_is_counted_from_the_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def git(*args):
                subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
                                *args], check=True, capture_output=True)

            def commit(path, text):
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
                git("add", "-A")
                git("commit", "-q", "-m", "change " + path)

            git("init", "-q")
            commit("first.py", "# 1\n")           # first arrival -> 0.1.0
            commit("first.py", "# 2\n")           # patch
            commit("second.py", "# 1\n")          # arrival -> 0.2.0
            commit("tests/test_x.py", "# t\n")    # tests are not a capability -> patch
            commit("build-exe.py", "# b\n")       # neither is the build -> patch
            found = version.from_git(root)
        self.assertEqual((found.name, found.build), ("0.2.2", "5"))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(version.write_file(Path(tmp)), "no history, nothing written")

    def test_version_flag_answers_without_a_display(self):
        environment = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
        environment[version.ENVIRONMENT_VARIABLE] = "1.2.3 45 abcdef0 2026-01-02"
        done = subprocess.run(
            [sys.executable, str(ENTRY), "--version"],
            capture_output=True, text=True, env=environment, timeout=60, check=False, cwd=str(ROOT),
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Freeze Guard 1.2.3 (45)", done.stdout)


# ----------------------------------------------------------------------
# Child processes
# ----------------------------------------------------------------------
class ChildEnvironmentTests(unittest.TestCase):
    def test_children_get_no_paths_into_the_unpacked_files(self):
        """Otherwise the tools the program starts load the program's libraries."""
        env = {
            "LD_LIBRARY_PATH": "/tmp/_MEIabc",
            "LD_LIBRARY_PATH_ORIG": "/opt/lib",
            "TCL_LIBRARY": "/tmp/_MEIabc/_tcl_data",
            "XDG_DATA_DIRS": "/tmp/_MEIabc/share:/usr/share",
            "_PYI_ARCHIVE_FILE": "/home/me/freeze-guard",
            "HOME": "/home/me",
        }
        with mock.patch.object(exe_support, "IS_FROZEN", True), \
                mock.patch.object(sys, "_MEIPASS", "/tmp/_MEIabc", create=True):
            self.assertEqual(exe_support.child_environment(env), {
                "LD_LIBRARY_PATH": "/opt/lib",
                "XDG_DATA_DIRS": "/usr/share",
                "HOME": "/home/me",
            })
            self.assertNotIn("LD_LIBRARY_PATH",
                             exe_support.child_environment({"LD_LIBRARY_PATH": "/tmp/_MEIabc"}))

    def test_a_checkout_hands_its_environment_on_unchanged(self):
        with mock.patch.object(exe_support, "IS_FROZEN", False):
            self.assertEqual(exe_support.child_environment({"LD_LIBRARY_PATH": "/x", "A": "b"}),
                             {"LD_LIBRARY_PATH": "/x", "A": "b"})

    def test_the_executable_cleans_every_child_and_does_it_once(self):
        original = subprocess.Popen
        self.addCleanup(setattr, subprocess, "Popen", original)
        with mock.patch.object(exe_support, "IS_FROZEN", True), \
                mock.patch.object(sys, "_MEIPASS", "/tmp/_MEIabc", create=True), \
                mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "/tmp/_MEIabc",
                                             "LD_LIBRARY_PATH_ORIG": "/opt/lib"}):
            exe_support.use_system_environment_for_children()
            patched = subprocess.Popen
            exe_support.use_system_environment_for_children()
            self.assertIs(subprocess.Popen, patched)
            done = subprocess.run(["sh", "-c", "printf %s \"$LD_LIBRARY_PATH\""],
                                  capture_output=True, text=True, check=True)
        self.assertEqual(done.stdout, "/opt/lib")

    def test_outside_the_executable_nothing_is_patched(self):
        original = subprocess.Popen
        with mock.patch.object(exe_support, "IS_FROZEN", False):
            exe_support.use_system_environment_for_children()
        self.assertIs(subprocess.Popen, original)


# ----------------------------------------------------------------------
# No interpreter calls when frozen
# ----------------------------------------------------------------------
class RelaunchTests(FrozenTestCase):
    def test_the_program_is_started_again_through_itself(self):
        self.assertEqual(exe_support.relaunch_command("/x/run.py", "--helper"),
                         [str(self.executable.resolve()), "--helper"])

    def test_a_checkout_starts_it_through_python(self):
        with mock.patch.object(exe_support, "IS_FROZEN", False):
            self.assertEqual(exe_support.relaunch_command("/x/run.py", "--helper"),
                             [str(self.executable), "-u", "/x/run.py", "--helper"])


# ----------------------------------------------------------------------
# Freeze Guard in the executable
# ----------------------------------------------------------------------
HAS_GI = importlib.util.find_spec("gi") is not None


class DataFileTests(unittest.TestCase):
    def test_the_monitor_is_carried_because_the_window_loads_it_by_path(self):
        self.assertIn("run-monitor.py", BUILD.DATA_FILES)

    def test_the_modules_the_monitor_imports_are_carried(self):
        """The monitor is read as a file, so PyInstaller does not see its imports."""
        text = (ROOT / "run-monitor.py").read_text(encoding="utf-8")
        for module in ("curses",):
            self.assertIn("import " + module, text)
            self.assertIn(module, BUILD.HIDDEN_IMPORTS)


@unittest.skipUnless(HAS_GI and os.path.exists("/proc/pressure/memory"), "needs PyGObject and PSI")
class MonitorFlagTests(unittest.TestCase):
    def test_the_terminal_dashboard_runs_through_the_window_script(self):
        """``--monitor`` is how the executable shows the dashboard: it has no second command."""
        done = subprocess.run(
            [sys.executable, str(ENTRY), "--monitor", "--once"],
            capture_output=True, text=True, timeout=60, check=False, cwd=str(ROOT),
            env={k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")},
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(done.stdout.strip(), "no snapshot printed")


if __name__ == "__main__":
    unittest.main()
