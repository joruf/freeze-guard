"""OS test: the documented platform matrix in fg_platform.py.

Nothing here switches operating system - the profiles describe machines
(kernel without PSI, no systemd, container, Wayland, Windows, macOS) and the
matrix says what works there. The last class checks the running machine against
the same rules, which is what `freeze-guard doctor` prints.
"""

from __future__ import annotations

import subprocess
import sys
import unittest

import fg_platform as P
from tests.fg_test_utils import ROOT, has_procfs

FULL, PART, NONE = P.SupportLevel.FULL, P.SupportLevel.PARTIAL, P.SupportLevel.UNSUPPORTED


class TestProfileCoverage(unittest.TestCase):
    """The matrix has to cover the cases we actually reason about."""

    def test_known_profiles_include_the_documented_machines(self) -> None:
        for name in ("linux_full", "linux_wayland", "linux_no_psi",
                     "linux_no_systemd", "linux_cgroup_v1", "linux_container",
                     "linux_headless", "windows", "darwin"):
            self.assertIn(name, P.KNOWN_PROFILES)

    def test_every_profile_answers_every_feature(self) -> None:
        for name, ctx in P.KNOWN_PROFILES.items():
            caps = P.evaluate_capabilities(ctx)
            with self.subTest(profile=name):
                self.assertEqual(set(caps), set(P.FEATURES))
                for feature, cap in caps.items():
                    self.assertIsInstance(cap.level, P.SupportLevel)
                    self.assertTrue(cap.detail.strip(), feature)

    def test_core_features_are_the_three_that_define_the_tool(self) -> None:
        self.assertEqual(set(P.CORE_FEATURES), {"monitor", "recorder", "guard_kill"})


class TestLinuxProfiles(unittest.TestCase):
    """Where Linux differs from Linux."""

    def test_reference_machine_supports_everything(self) -> None:
        caps = P.evaluate_capabilities(P.LINUX_FULL)
        for feature, cap in caps.items():
            with self.subTest(feature=feature):
                self.assertEqual(cap.level, FULL, cap.detail)

    def test_wayland_is_as_capable_as_x11(self) -> None:
        """The dashboard only reads /proc and draws, so the session type is
        irrelevant to it - only the tray backend differs."""
        wayland = P.evaluate_capabilities(P.LINUX_WAYLAND)
        self.assertEqual(wayland["gui"].level, FULL)
        self.assertEqual(wayland["monitor"].level, FULL)
        self.assertEqual(wayland["tray"].level, FULL)

    def test_without_psi_the_core_is_gone(self) -> None:
        caps = P.evaluate_capabilities(P.LINUX_NO_PSI)
        for feature in P.CORE_FEATURES:
            self.assertEqual(caps[feature].level, NONE)
        self.assertIn("PSI", caps["monitor"].detail)
        # ...but the parts that do not depend on PSI still work
        self.assertEqual(caps["earlyoom"].level, FULL)
        self.assertEqual(caps["gui"].level, FULL)

    def test_without_systemd_the_switches_go_but_the_numbers_stay(self) -> None:
        caps = P.evaluate_capabilities(P.LINUX_NO_SYSTEMD)
        self.assertEqual(caps["service_switch"].level, NONE)
        self.assertEqual(caps["monitor"].level, FULL)
        self.assertEqual(caps["recorder"].level, FULL)
        self.assertEqual(caps["tray"].level, PART)  # Gtk.StatusIcon fallback

    def test_cgroup_v1_degrades_only_the_state_display(self) -> None:
        caps = P.evaluate_capabilities(P.LINUX_CGROUP_V1)
        self.assertEqual(caps["service_switch"].level, PART)
        self.assertIn("cgroup v2", caps["service_switch"].detail)
        self.assertEqual(caps["guard_kill"].level, FULL)

    def test_container_keeps_the_recorder_and_loses_the_rest(self) -> None:
        caps = P.evaluate_capabilities(P.LINUX_CONTAINER)
        self.assertEqual(caps["recorder"].level, FULL)
        self.assertEqual(caps["kernel_stalls"].level, PART)
        self.assertEqual(caps["gui"].level, NONE)
        self.assertEqual(caps["tray"].level, NONE)
        self.assertEqual(caps["auto_raise"].level, NONE)
        self.assertEqual(caps["service_switch"].level, NONE)

    def test_headless_server_runs_the_console_tools(self) -> None:
        caps = P.evaluate_capabilities(P.LINUX_HEADLESS)
        self.assertEqual(caps["monitor"].level, FULL)
        self.assertEqual(caps["guard_kill"].level, FULL)
        self.assertEqual(caps["gui"].level, NONE)

    def test_unreadable_kmsg_only_costs_the_stall_counter(self) -> None:
        ctx = P.PlatformContext(name="no_kmsg", kmsg_readable=False)
        caps = P.evaluate_capabilities(ctx)
        self.assertEqual(caps["kernel_stalls"].level, PART)
        self.assertEqual(caps["monitor"].level, FULL)

    def test_without_pkexec_only_own_processes_can_be_ended(self) -> None:
        ctx = P.PlatformContext(name="no_pkexec", has_pkexec=False)
        caps = P.evaluate_capabilities(ctx)
        self.assertEqual(caps["process_kill"].level, PART)
        self.assertEqual(caps["service_switch"].level, PART)
        self.assertIn("terminal", caps["service_switch"].detail)

    def test_missing_gtk_removes_the_window_but_not_the_protection(self) -> None:
        ctx = P.PlatformContext(name="no_gtk", has_gtk=False, tray_backend="none")
        caps = P.evaluate_capabilities(ctx)
        self.assertEqual(caps["gui"].level, NONE)
        self.assertEqual(caps["guard_kill"].level, FULL)


class TestNonLinux(unittest.TestCase):
    """Windows and macOS are out of scope, and the matrix says so plainly."""

    def test_windows_supports_nothing(self) -> None:
        caps = P.evaluate_capabilities(P.WINDOWS)
        for feature, cap in caps.items():
            with self.subTest(feature=feature):
                self.assertEqual(cap.level, NONE)
                self.assertIn("/proc", cap.detail)

    def test_macos_supports_nothing(self) -> None:
        caps = P.evaluate_capabilities(P.DARWIN)
        self.assertTrue(all(c.level is NONE for c in caps.values()))

    def test_profiles_carry_a_reason(self) -> None:
        for ctx in (P.WINDOWS, P.DARWIN, P.LINUX_NO_PSI):
            self.assertTrue(ctx.notes)


class TestTrayMatrix(unittest.TestCase):
    """Three tray backends with three different verdicts."""

    def test_backends_map_to_levels(self) -> None:
        expected = {"xapp": FULL, "appindicator": FULL, "statusicon": PART,
                    "none": NONE}
        for backend, level in expected.items():
            ctx = P.PlatformContext(name=f"tray_{backend}", tray_backend=backend)
            with self.subTest(backend=backend):
                self.assertEqual(P.evaluate_capabilities(ctx)["tray"].level, level)

    def test_only_xapp_promises_a_label(self) -> None:
        ctx = P.PlatformContext(name="tray_xapp", tray_backend="xapp")
        self.assertIn("label", P.evaluate_capabilities(ctx)["tray"].detail)

    def test_without_a_tray_the_window_cannot_hide(self) -> None:
        ctx = P.PlatformContext(name="tray_none", tray_backend="none")
        caps = P.evaluate_capabilities(ctx)
        self.assertEqual(caps["auto_raise"].level, PART)
        self.assertIn("visible", caps["auto_raise"].detail)


@unittest.skipUnless(has_procfs(), "needs /proc")
class TestThisMachine(unittest.TestCase):
    """Probing must be read-only and must agree with the matrix."""

    def test_current_context_is_probed_without_side_effects(self) -> None:
        ctx = P.current_context()
        self.assertEqual(ctx.name, "current")
        self.assertEqual(ctx.os_family, "linux")
        self.assertIn(ctx.session, ("x11", "wayland", "none"))
        self.assertIn(ctx.tray_backend, ("xapp", "appindicator", "statusicon", "none"))
        self.assertIn(ctx.cgroup_version, (1, 2))

    def test_report_mentions_every_feature(self) -> None:
        text = P.report(P.current_context())
        for feature in P.FEATURES:
            self.assertIn(feature, text)

    def test_report_marks_each_line(self) -> None:
        for line in P.report(P.LINUX_FULL).splitlines():
            if line.strip().startswith("["):
                self.assertRegex(line.strip(), r"^\[[+~-]\]")

    def test_doctor_command_runs_and_exits_by_the_matrix(self) -> None:
        result = subprocess.run([sys.executable, "fg_platform.py"], cwd=ROOT,
                                capture_output=True, text=True, timeout=60)
        self.assertIn("platform report", result.stdout)
        caps = P.evaluate_capabilities(P.current_context())
        broken = any(caps[f].level is NONE for f in P.CORE_FEATURES)
        self.assertEqual(result.returncode, 1 if broken else 0, result.stdout)

    def test_cli_exposes_the_report(self) -> None:
        result = subprocess.run(["./freeze-guard", "doctor"], cwd=ROOT,
                                capture_output=True, text=True, timeout=60)
        self.assertIn("platform report", result.stdout)


if __name__ == "__main__":
    unittest.main()
