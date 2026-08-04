"""End-to-end tests for the dashboard.

A real Window with real widgets is built and driven through an incident with
samples the test controls: green, tight memory, warning, critical, back to calm.
The sampling thread and the tray icon are stubbed - not to avoid the work, but
because a test must not depend on the machine's mood or add an icon to the user's
panel while it runs.

The samples themselves come from the shipped Sampler, so the contract between
measurement and display is part of what is tested.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from tests.fg_test_utils import (build_window, gui_testable, load_gui, load_monitor,
                                 pump, scenario_sample, write_history)

if gui_testable():
    G = load_gui()
    M = load_monitor()


@unittest.skipUnless(gui_testable(), "needs GTK, a display and PSI")
class TestDashboardIncident(unittest.TestCase):
    """One window, one incident, from calm to critical and back."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.samples = {name: scenario_sample(M, name)
                       for name in ("green", "yellow", "orange", "red")}

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.history = os.path.join(self.tmp.name, "history.csv")
        write_history(self.history, samples=40, gap_after=20, gap_seconds=600,
                      alarm_from=16)
        self.window, self.poller, self.tray = build_window(
            G, self.tmp.name, settings_overrides={"history_file": self.history})
        pump()

    def tearDown(self) -> None:
        self.window.destroy()
        pump()
        self.tmp.cleanup()

    # -- the pages exist ----------------------------------------------------
    def test_all_five_pages_are_present(self) -> None:
        self.assertEqual(self.window.notebook.get_n_pages(), 5)
        self.assertEqual(G.Window.PAGES,
                         ("live", "history", "log", "sim", "settings"))

    def test_the_poller_was_started_with_the_configured_interval(self) -> None:
        self.assertTrue(self.poller.started)
        self.assertEqual(self.poller.interval, 5.0)

    # -- the live page reacts ------------------------------------------------
    def test_a_calm_sample_paints_green_and_fills_the_tables(self) -> None:
        self.poller.feed(self.samples["green"])
        pump()
        self.assertEqual(self.window.banner_level.get_text(), G.t("level.ok"))
        self.assertEqual(self.window.banner_icon.get_text(), G.LEVEL_ICON[G.GREEN])
        self.assertTrue(self.window.live.mem_table.store)
        self.assertIn("%", self.window.live.ram_caption.get_text())

    def test_tight_memory_is_raised_not_critical(self) -> None:
        self.poller.feed(self.samples["yellow"])
        pump()
        self.assertEqual(self.window.banner_level.get_text(), G.t("level.raised"))
        self.assertEqual(self.window.banner_reason.get_text(),
                         G.STR[G.LANG]["reason.tight"])

    def test_critical_sample_paints_critical_everywhere(self) -> None:
        self.poller.feed(self.samples["red"])
        pump()
        self.assertEqual(self.window.banner_level.get_text(), G.t("level.critical"))
        classes = self.window.banner.get_style_context().list_classes()
        self.assertIn("fg-banner-critical", classes)
        self.assertNotIn("fg-banner-good", classes)
        self.assertEqual(self.tray.updates[-1][1], "critical")

    def test_banner_class_is_exchanged_not_accumulated(self) -> None:
        for name in ("green", "red", "green"):
            self.poller.feed(self.samples[name])
            pump()
        classes = [c for c in self.window.banner.get_style_context().list_classes()
                   if c.startswith("fg-banner-")]
        self.assertEqual(classes, ["fg-banner-good"])

    def test_charts_collect_one_point_per_sample(self) -> None:
        for _ in range(3):
            self.poller.feed(self.samples["green"])
        pump()
        self.assertEqual(len(self.window.live.psi_chart.times), 3)
        self.assertEqual(len(self.window.live.swap_chart.times), 3)
        self.assertEqual(len(self.window.live.cpu_chart.times), 3)

    def test_tray_label_carries_the_free_memory(self) -> None:
        self.poller.feed(self.samples["green"])
        pump()
        self.assertTrue(self.tray.updates[-1][2].endswith("%"))

    def test_panel_label_can_be_switched_off(self) -> None:
        self.window.settings.data["panel_label"] = False
        self.poller.feed(self.samples["green"])
        pump()
        self.assertEqual(self.tray.updates[-1][2], "")

    def test_pause_freezes_the_clock_but_keeps_the_level(self) -> None:
        self.window.pause.set_active(True)
        pump()
        self.assertTrue(self.poller.paused)
        self.assertEqual(self.window.banner_time.get_text(), G.t("hdr.paused"))
        self.window.pause.set_active(False)
        pump()
        self.assertFalse(self.poller.paused)

    # -- the automatic pop-up ------------------------------------------------
    def test_window_raises_itself_once_per_incident(self) -> None:
        """Re-arming only after the level dropped is the whole point: a flapping
        PSI value must not steal focus every few seconds."""
        raises = []
        self.window.present_window = lambda: raises.append(1)
        self.window.hide()
        pump()

        self.poller.feed(self.samples["red"])
        self.poller.feed(self.samples["red"])
        self.poller.feed(self.samples["orange"])
        pump()
        self.assertEqual(len(raises), 1)

        self.poller.feed(self.samples["green"])   # incident over, re-arm
        self.poller.feed(self.samples["red"])
        pump()
        self.assertEqual(len(raises), 2)

    def test_no_raise_below_the_configured_level(self) -> None:
        raises = []
        self.window.present_window = lambda: raises.append(1)
        self.window.hide()
        self.window.settings.data["auto_open_level"] = G.RED
        self.poller.feed(self.samples["orange"])
        pump()
        self.assertEqual(raises, [])
        self.poller.feed(self.samples["red"])
        pump()
        self.assertEqual(len(raises), 1)

    def test_auto_open_can_be_switched_off(self) -> None:
        raises = []
        self.window.present_window = lambda: raises.append(1)
        self.window.hide()
        self.window.settings.data["auto_open"] = False
        self.poller.feed(self.samples["red"])
        pump()
        self.assertEqual(raises, [])

    def test_a_visible_window_is_not_raised_again(self) -> None:
        raises = []
        self.window.present_window = lambda: raises.append(1)
        self.window.show_all()
        pump()
        self.poller.feed(self.samples["red"])
        pump()
        self.assertEqual(raises, [])

    # -- closing versus quitting --------------------------------------------
    def test_close_hides_while_a_tray_exists(self) -> None:
        self.window.show_all()
        pump()
        self.assertTrue(self.window.emit("delete-event", None))
        pump()
        self.assertFalse(self.window.get_visible())
        self.assertFalse(self.poller.stopped)

    def test_without_a_tray_closing_really_quits(self) -> None:
        window, poller, _tray = build_window(G, self.tmp.name, tray=False)
        window.show_all()
        pump()
        self.assertFalse(window.emit("delete-event", None))
        self.assertTrue(poller.stopped)
        window.destroy()

    # -- language ------------------------------------------------------------
    def test_language_switch_rebuilds_the_whole_window(self) -> None:
        self.poller.feed(self.samples["yellow"])
        pump()
        self.window.on_setting("lang", "de")
        pump()
        self.assertEqual(G.LANG, "de")
        self.assertEqual(self.window.banner_level.get_text(), "ERHÖHT")
        self.assertEqual(self.window.banner_reason.get_text(),
                         G.STR["de"]["reason.tight"])
        self.assertEqual(self.window.notebook.get_nth_page(0).get_parent(),
                         self.window.notebook)
        self.assertEqual(self.tray.rebuilds, 1)
        # and it keeps working afterwards
        self.poller.feed(self.samples["red"])
        pump()
        self.assertEqual(self.window.banner_level.get_text(), "KRITISCH")
        self.window.on_setting("lang", "en")
        pump()

    def test_language_switch_keeps_the_current_page(self) -> None:
        self.window.notebook.set_current_page(3)
        pump()
        self.window.on_setting("lang", "de")
        pump()
        self.assertEqual(self.window.notebook.get_current_page(), 3)
        self.window.on_setting("lang", "en")
        pump()

    def test_interval_setting_reaches_the_poller(self) -> None:
        self.window.on_setting("interval", 10.0)
        self.assertEqual(self.window.poller.interval, 10.0)

    # -- the other pages -----------------------------------------------------
    def test_history_page_shows_the_outage_from_the_recording(self) -> None:
        rows = [row[0] for row in self.window.history.store]
        self.assertTrue(any(G.t("hist.outage").split()[0] in row for row in rows))
        self.assertIn("min", " ".join(row[1] for row in self.window.history.store))
        self.assertIn("1", self.window.history.summary.get_text())

    def test_history_page_reloads_for_another_period(self) -> None:
        self.window.history.range_combo.set_active(0)   # 6 hours
        pump()
        self.assertTrue(self.window.history.summary.get_text())

    def test_history_page_without_a_recording_explains_itself(self) -> None:
        self.window.history.path = os.path.join(self.tmp.name, "gone.csv")
        self.window.history.reload()
        pump()
        self.assertIn("gone.csv", self.window.history.missing_title.get_text())
        self.assertIn("freeze-guard-recorder",
                      self.window.history.missing_text.get_text())

    def test_simulation_page_names_a_target(self) -> None:
        self.window.notebook.set_current_page(3)
        pump()
        self.poller.feed(self.samples["red"])
        pump()
        self.assertTrue(len(self.window.sim.table.store) > 0)
        self.assertTrue(self.window.sim.target.get_text())

    def test_simulation_can_be_frozen(self) -> None:
        self.window.notebook.set_current_page(3)
        self.window.sim.auto.set_active(False)
        self.poller.feed(self.samples["red"])
        pump()
        self.assertEqual(len(self.window.sim.table.store), 0)

    def test_settings_page_mirrors_the_settings(self) -> None:
        page = self.window.settings_page
        self.assertEqual(page.lang_combo.get_active(), 0)          # English
        self.assertEqual(page.interval_combo.get_active(), 2)      # 5 s
        self.assertTrue(page.tray_switch.get_active())
        self.assertTrue(page.auto_switch.get_active())

    def test_changing_the_interval_in_the_ui_persists_it(self) -> None:
        page = self.window.settings_page
        page.interval_combo.set_active(0)     # 1 s
        pump()
        self.assertEqual(self.window.settings["interval"], 1.0)
        self.assertEqual(self.window.poller.interval, 1.0)
        self.assertEqual(G.Settings()["interval"], 1.0)

    def test_autostart_switch_writes_and_removes_the_file(self) -> None:
        page = self.window.settings_page
        page.autostart_switch.set_active(True)
        pump()
        self.assertTrue(os.path.exists(G.AUTOSTART_FILE))
        page.autostart_switch.set_active(False)
        pump()
        self.assertFalse(os.path.exists(G.AUTOSTART_FILE))

    # -- notifications -------------------------------------------------------
    def notification_spy(self):
        """Records what would be sent instead of sending it."""
        sent, closed = [], []
        self.window._send_notification = lambda level, reason: sent.append((level, reason))
        self.window._close_notification = lambda: closed.append(1)
        return sent, closed

    def test_calm_never_notifies(self) -> None:
        sent, _closed = self.notification_spy()
        self.poller.feed(self.samples["green"])
        self.poller.feed(self.samples["yellow"])
        pump()
        self.assertEqual(sent, [])

    def test_warning_notifies_once_not_on_every_sample(self) -> None:
        sent, _closed = self.notification_spy()
        for _ in range(5):
            self.poller.feed(self.samples["orange"])
        pump()
        self.assertEqual(len(sent), 1)

    def test_flapping_between_two_levels_does_not_spam(self) -> None:
        """This is what actually happened: the level bounced between raised and
        warning, and every bounce produced a notification that never expired."""
        sent, _closed = self.notification_spy()
        for _ in range(6):
            self.poller.feed(self.samples["orange"])
            self.poller.feed(self.samples["yellow"])
        pump()
        self.assertEqual(len(sent), 1)

    def test_escalation_within_the_cooldown_is_held_back(self) -> None:
        sent, _closed = self.notification_spy()
        self.poller.feed(self.samples["orange"])
        self.poller.feed(self.samples["red"])
        pump()
        self.assertEqual(len(sent), 1)

    def test_escalation_after_the_cooldown_is_announced(self) -> None:
        sent, _closed = self.notification_spy()
        self.poller.feed(self.samples["orange"])
        pump()
        self.window._last_notify -= self.window.settings["notify_cooldown"] + 1
        self.poller.feed(self.samples["red"])
        pump()
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[1][0], G.t("level.critical"))

    def test_recovery_closes_the_bubble(self) -> None:
        sent, closed = self.notification_spy()
        self.poller.feed(self.samples["red"])
        pump()
        self.poller.feed(self.samples["green"])
        pump()
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(closed), 1)

    def test_a_second_episode_notifies_again(self) -> None:
        sent, _closed = self.notification_spy()
        self.poller.feed(self.samples["red"])
        self.poller.feed(self.samples["green"])
        pump()
        self.window._last_notify -= self.window.settings["notify_cooldown"] + 1
        self.poller.feed(self.samples["red"])
        pump()
        self.assertEqual(len(sent), 2)

    def test_notifications_can_be_switched_off(self) -> None:
        sent, _closed = self.notification_spy()
        self.window.settings.data["notify"] = False
        self.poller.feed(self.samples["red"])
        pump()
        self.assertEqual(sent, [])

    def test_closing_the_window_explains_the_tray_once(self) -> None:
        hints = []
        self.window._hint_notification = lambda title, body: hints.append(title)
        self.window.show_all()
        pump()
        self.window.emit("delete-event", None)
        pump()
        self.assertEqual(len(hints), 1)
        self.window.show_all()
        self.window.emit("delete-event", None)
        pump()
        self.assertEqual(len(hints), 1, "the hint must not repeat")

    def test_status_messages_are_transient(self) -> None:
        self.window.status("hello")
        self.assertEqual(self.window.banner_status.get_text(), "hello")
        self.window._clear_status()
        self.assertEqual(self.window.banner_status.get_text(), "")

    def test_kmsg_without_permission_is_shown_as_unknown(self) -> None:
        self.window.live.set_kmsg_unavailable()
        self.poller.feed(self.samples["green"])
        pump()
        value, sub = self.window.live.tiles["stalls"]
        self.assertEqual(value.get_text(), "–")
        self.assertEqual(sub.get_text(), G.t("tile.stalls_denied"))


@unittest.skipUnless(gui_testable(), "needs GTK, a display and PSI")
class TestProtectionCard(unittest.TestCase):
    """The card must never be a dead end: either it switches or it installs."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.window, self.poller, _tray = build_window(G, self.tmp.name)
        self.installed = set()
        self.active = set()
        self._real_installed = G.unit_installed
        self._real_active = G.M.service_active
        G.unit_installed = lambda unit: unit in self.installed
        G.M.service_active = lambda unit: unit in self.active
        pump()

    def tearDown(self) -> None:
        G.unit_installed = self._real_installed
        G.M.service_active = self._real_active
        self.window.destroy()
        pump()
        self.tmp.cleanup()

    def units(self):
        return [unit for unit, _label in G.UNITS]

    def test_nothing_installed_offers_the_installer(self) -> None:
        self.window.live._update_services()
        pump()
        self.assertTrue(self.window.live._install_mode)
        self.assertEqual(self.window.live.enable_all.get_label(), G.t("svc.install"))
        self.assertTrue(self.window.live.enable_all.get_sensitive())
        self.assertTrue(self.window.live.svc_hint.get_text())

    def test_switches_stay_off_and_explain_themselves(self) -> None:
        self.window.live._update_services()
        pump()
        for unit in self.units():
            _icon, state, switch, _handler = self.window.live.services[unit]
            self.assertFalse(switch.get_sensitive())
            self.assertEqual(state.get_text(), G.t("svc.missing"))
            self.assertIn("install", switch.get_tooltip_text().lower())

    def test_all_installed_and_running_shows_active(self) -> None:
        self.installed.update(self.units())
        self.active.update(self.units())
        self.window.live._update_services()
        pump()
        self.assertFalse(self.window.live._install_mode)
        self.assertEqual(self.window.live.enable_all.get_label(), G.t("svc.enable_all"))
        for unit in self.units():
            _icon, state, switch, _handler = self.window.live.services[unit]
            self.assertTrue(switch.get_active())
            self.assertEqual(state.get_text(), G.t("svc.active"))

    def test_installed_but_stopped_shows_stopped(self) -> None:
        self.installed.update(self.units())
        self.window.live._update_services()
        pump()
        for unit in self.units():
            _icon, state, switch, _handler = self.window.live.services[unit]
            self.assertFalse(switch.get_active())
            self.assertEqual(state.get_text(), G.t("svc.stopped"))

    def test_one_missing_unit_is_named(self) -> None:
        self.installed.update(self.units()[1:])
        self.active.update(self.units()[1:])
        self.window.live._update_services()
        pump()
        self.assertFalse(self.window.live._install_mode)
        self.assertIn(self.units()[0], self.window.live.svc_hint.get_text())

    def test_reflecting_reality_does_not_trigger_systemctl(self) -> None:
        """The state is refreshed on every sample; if that looked like a user
        toggle, the dashboard would fire pkexec once a second."""
        calls = []
        real = G.systemctl
        G.systemctl = lambda action, units, done: calls.append((action, tuple(units)))
        try:
            self.installed.update(self.units())
            self.active.update(self.units())
            for _ in range(3):
                self.window.live._update_services()
                pump()
            self.assertEqual(calls, [])
        finally:
            G.systemctl = real

    def test_flipping_a_switch_calls_systemctl_once(self) -> None:
        calls = []
        real = G.systemctl
        G.systemctl = lambda action, units, done: calls.append((action, tuple(units)))
        try:
            self.installed.update(self.units())
            self.window.live._update_services()
            pump()
            _icon, _state, switch, _handler = self.window.live.services["earlyoom"]
            switch.set_active(True)
            pump()
            self.assertEqual(calls, [("enable", ("earlyoom",))])
        finally:
            G.systemctl = real

    def test_enable_all_switches_every_installed_unit(self) -> None:
        calls = []
        real = G.systemctl
        G.systemctl = lambda action, units, done: calls.append((action, tuple(units)))
        try:
            self.installed.update(self.units())
            self.window.live._update_services()
            pump()
            self.window.live.enable_all.clicked()
            pump()
            self.assertEqual(calls, [("enable", tuple(self.units()))])
        finally:
            G.systemctl = real

    def test_a_failed_switch_is_reported(self) -> None:
        self.installed.update(self.units())
        self.window.live._switch_done("earlyoom", "Authentication failed")
        pump()
        self.assertIn("earlyoom", self.window.banner_status.get_text())


if __name__ == "__main__":
    unittest.main()
