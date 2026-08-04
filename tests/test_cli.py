"""Tests for the command line surface.

The CLI is the part users type, so it is tested the way it is used: as a
subprocess, with exit codes. Nothing here needs a display.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest

from tests.fg_test_utils import (CLI_PATH, INSTALL_PATH, MONITOR_PATH, ROOT,
                                 has_gtk, has_psi, run_cli, run_monitor_cli,
                                 write_history)


class TestCliHelp(unittest.TestCase):
    """Help and dispatch."""

    def test_help_lists_every_command(self) -> None:
        result = run_cli("--help")
        self.assertEqual(result.returncode, 0)
        for command in ("status", "monitor", "ui", "history", "log", "dryrun",
                        "doctor", "uninstall"):
            self.assertIn(f"freeze-guard {command}", result.stdout)

    def test_help_documents_the_short_alias(self) -> None:
        self.assertIn("vfg", run_cli("--help").stdout)

    def test_unknown_command_fails_with_usage(self) -> None:
        result = run_cli("frobnicate")
        self.assertEqual(result.returncode, 1)
        self.assertIn("freeze-guard status", result.stdout)

    def test_piped_invocation_without_arguments_prints_status(self) -> None:
        """No tty means no interactive menu: scripts that used to call bare
        `freeze-guard` must keep getting a status dump."""
        result = run_cli()
        self.assertEqual(result.returncode, 0)
        self.assertIn("Speicher", result.stdout)

    def test_uninstall_without_root_refuses(self) -> None:
        if os.getuid() == 0:
            self.skipTest("running as root")
        result = run_cli("uninstall")
        self.assertEqual(result.returncode, 1)
        self.assertIn("sudo", result.stdout)


class TestCliStatus(unittest.TestCase):
    """`status` is the one command that has to work on any machine."""

    def test_status_reports_the_sections(self) -> None:
        result = run_cli("status")
        self.assertEqual(result.returncode, 0)
        for section in ("Speicher", "Swap", "Speicherdruck", "Dienste"):
            self.assertIn(section, result.stdout)

    def test_status_names_the_kill_thresholds(self) -> None:
        out = run_cli("status").stdout
        self.assertIn("20%", out)
        self.assertIn("15%", out)

    def test_dryrun_explains_its_rule(self) -> None:
        result = run_cli("dryrun")
        self.assertEqual(result.returncode, 0)
        self.assertIn("200 MB", result.stdout)
        self.assertIn("Simulation", result.stdout)

    def test_doctor_prints_the_matrix(self) -> None:
        result = run_cli("doctor")
        self.assertIn("platform report", result.stdout)
        self.assertIn("monitor", result.stdout)


class TestMonitorCli(unittest.TestCase):
    """run-monitor.py without a terminal must degrade, not die."""

    @unittest.skipUnless(has_psi(), "needs PSI")
    def test_once_prints_a_snapshot(self) -> None:
        result = run_monitor_cli("--once")
        self.assertEqual(result.returncode, 0)
        for token in ("RAM", "Druck", "Auslagern", "CPU", "Schutz"):
            self.assertIn(token, result.stdout)

    @unittest.skipUnless(has_psi(), "needs PSI")
    def test_once_shows_an_alert_level(self) -> None:
        out = run_monitor_cli("--once").stdout
        self.assertTrue(any(f"[{name}]" in out
                            for name in ("OK", "ERHOEHT", "WARNUNG", "KRITISCH")))

    def test_help_documents_the_modes(self) -> None:
        result = run_monitor_cli("--help")
        self.assertEqual(result.returncode, 0)
        for flag in ("--log", "--history", "--plain", "--once", "--interval", "--file"):
            self.assertIn(flag, result.stdout)

    def test_history_of_a_missing_file_explains_itself(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_monitor_cli("--history", "--file",
                                     os.path.join(tmp, "nope.csv"))
            self.assertEqual(result.returncode, 0)
            self.assertIn("Keine Aufzeichnung", result.stdout)
            self.assertIn("freeze-guard-recorder", result.stdout)

    def test_history_finds_the_outage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "history.csv")
            write_history(path, samples=40, gap_after=20, gap_seconds=600,
                          alarm_from=15)
            result = run_monitor_cli("--history", "--file", path, "--hours", "48")
            self.assertEqual(result.returncode, 0)
            self.assertIn("Ausfall", result.stdout)
            self.assertIn("10 min tot", result.stdout)
            self.assertIn("KRITISCH", result.stdout)

    def test_history_reports_a_calm_period(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "history.csv")
            write_history(path, samples=20)
            out = run_monitor_cli("--history", "--file", path).stdout
            self.assertIn("Kein Ausfall", out)

    def test_process_name_is_set_for_the_whitelist(self) -> None:
        """The guard whitelists by comm; a Python script reports "python3" unless
        it renames itself, and "python3" is far too broad to whitelist."""
        code = (
            "import importlib.machinery as m, importlib.util as u, sys;"
            "sys.dont_write_bytecode=True;"
            f"l=m.SourceFileLoader('mon', {MONITOR_PATH!r});"
            "mod=u.module_from_spec(u.spec_from_loader(l.name, l));"
            "l.exec_module(mod);"
            "mod.set_process_name('fg-monitor');"
            "print(open('/proc/self/comm').read().strip())"
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.stdout.strip(), "fg-monitor", result.stderr)


class TestDashboardEntryPoint(unittest.TestCase):
    """run.py's own argument handling, without opening a window."""

    def run_gui(self, *args, env=None):
        return subprocess.run([sys.executable, os.path.join(ROOT, "run.py"), *args],
                              capture_output=True, text=True, timeout=60, env=env)

    @unittest.skipUnless(has_gtk(), "needs PyGObject")
    def test_help_documents_the_options(self) -> None:
        result = self.run_gui("--help")
        self.assertEqual(result.returncode, 0)
        for flag in ("--interval", "--file", "--lang", "--page", "--window"):
            self.assertIn(flag, result.stdout)

    @unittest.skipUnless(has_gtk(), "needs PyGObject")
    def test_unknown_page_is_rejected(self) -> None:
        result = self.run_gui("--page", "nonsense")
        self.assertEqual(result.returncode, 2)

    @unittest.skipUnless(has_gtk(), "needs PyGObject")
    def test_without_a_display_it_points_at_the_console(self) -> None:
        env = {k: v for k, v in os.environ.items()
               if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
        result = self.run_gui(env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("freeze-guard monitor", result.stderr + result.stdout)

    @unittest.skipUnless(has_gtk(), "needs PyGObject")
    def test_the_monitor_is_found_under_its_repository_name(self) -> None:
        """run.py imports run-monitor.py by path; a rename that forgets this
        would only show up at startup."""
        result = self.run_gui("--help")
        self.assertNotIn("not found", result.stderr)


class TestScriptsAreValid(unittest.TestCase):
    """Syntax of the shell parts, so a typo cannot reach a machine."""

    def test_shell_scripts_parse(self) -> None:
        for path in (CLI_PATH, INSTALL_PATH, os.path.join(ROOT, "fg-daemon.sh")):
            with self.subTest(script=os.path.basename(path)):
                result = subprocess.run(["bash", "-n", path], capture_output=True,
                                        text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_python_entry_points_compile(self) -> None:
        for name in ("run.py", "run-monitor.py", "fg_platform.py"):
            with self.subTest(script=name):
                result = subprocess.run(
                    [sys.executable, "-m", "py_compile", os.path.join(ROOT, name)],
                    capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_installer_refuses_without_root(self) -> None:
        if os.getuid() == 0:
            self.skipTest("running as root")
        result = subprocess.run(["bash", INSTALL_PATH], capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 1)
        self.assertIn("sudo", result.stdout)

    def test_installer_wires_the_renamed_entry_points(self) -> None:
        """The repository files are run.py / run-monitor.py, the installed
        commands are fg-gui / fg-monitor - the installer is the only place that
        knows both."""
        text = open(INSTALL_PATH).read()
        self.assertIn('install -m 755 "$SRC_DIR/run-monitor.py" /usr/local/bin/fg-monitor',
                      text)
        self.assertIn('ln -sf "$SRC_DIR/run.py" /usr/local/bin/fg-gui', text)

    def test_recorder_unit_uses_the_installed_name(self) -> None:
        text = open(INSTALL_PATH).read()
        self.assertIn("ExecStart=/usr/local/bin/fg-monitor --log", text)


if __name__ == "__main__":
    unittest.main()
