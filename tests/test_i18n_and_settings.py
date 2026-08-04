"""Unit tests for the two languages and the settings file.

Translation bugs are invisible in review and obvious to the user, so the parity
checks are mechanical: same keys, same placeholders, every key used in the code
present, every reason key shipped by the monitor translatable.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest

from tests.fg_test_utils import GUI_PATH, MONITOR_PATH, has_gtk, load_gui, load_monitor

M = load_monitor()
if has_gtk():
    G = load_gui()


@unittest.skipUnless(has_gtk(), "run.py needs PyGObject")
class TestTranslations(unittest.TestCase):
    """English is the default, German has to keep up with it."""

    def test_both_languages_exist_and_english_is_the_default(self) -> None:
        self.assertEqual(set(G.STR), {"en", "de"})
        self.assertEqual(G.Settings.DEFAULTS["lang"], "en")

    def test_key_sets_are_identical(self) -> None:
        en, de = set(G.STR["en"]), set(G.STR["de"])
        self.assertEqual(en - de, set(), "missing in German")
        self.assertEqual(de - en, set(), "missing in English")

    def test_placeholders_are_identical_per_key(self) -> None:
        for key in G.STR["en"]:
            with self.subTest(key=key):
                self.assertEqual(
                    set(re.findall(r"\{(\w+)\}", G.STR["en"][key])),
                    set(re.findall(r"\{(\w+)\}", G.STR["de"][key])))

    def test_no_translation_is_empty(self) -> None:
        for lang, table in G.STR.items():
            for key, value in table.items():
                with self.subTest(lang=lang, key=key):
                    self.assertTrue(value.strip(), key)

    def test_every_key_used_in_the_code_exists(self) -> None:
        source = open(GUI_PATH).read()
        used = set(re.findall(r"""(?<![\w.])t\(\s*["']([\w.]+)["']""", source))
        self.assertTrue(used)
        self.assertEqual(used - set(G.STR["en"]), set())

    def test_every_reason_from_the_monitor_is_translatable(self) -> None:
        """The monitor ships i18n keys for its alert reasons; a key without a
        translation would silently fall back to German in the English UI."""
        keys = set(re.findall(r'\("(reason\.\w+)"', open(MONITOR_PATH).read()))
        self.assertGreaterEqual(len(keys), 10)
        self.assertEqual(keys - set(G.STR["en"]), set())
        self.assertEqual(keys - set(G.STR["de"]), set())

    def test_lookup_falls_back_instead_of_raising(self) -> None:
        previous = G.LANG
        try:
            G.LANG = "de"
            self.assertEqual(G.t("level.warning"), "WARNUNG")
            G.LANG = "en"
            self.assertEqual(G.t("level.warning"), "WARNING")
            self.assertEqual(G.t("nope.missing"), "nope.missing")
        finally:
            G.LANG = previous

    def test_reason_text_prefers_the_key_over_the_shipped_sentence(self) -> None:
        previous = G.LANG
        try:
            G.LANG = "en"
            sample = {"reason": "Speicher wird knapp",
                      "reason_i18n": ("reason.tight", {})}
            self.assertEqual(G.reason_text(sample), G.STR["en"]["reason.tight"])
            # older monitor without the key: fall back to its text
            self.assertEqual(G.reason_text({"reason": "alt"}), "alt")
        finally:
            G.LANG = previous

    def test_numbers_follow_the_language(self) -> None:
        previous = G.LANG
        try:
            G.LANG = "de"
            self.assertEqual(G.num(12.34, 1), "12,3")
            G.LANG = "en"
            self.assertEqual(G.num(12.34, 1), "12.3")
        finally:
            G.LANG = previous

    def test_rates_switch_unit_at_ten_megabytes(self) -> None:
        previous = G.LANG
        try:
            G.LANG = "en"
            self.assertEqual(G.rate_text(0), "0 kB/s")
            self.assertEqual(G.rate_text(9000), "9000 kB/s")
            self.assertEqual(G.rate_text(20480), "20.0 MB/s")
        finally:
            G.LANG = previous

    def test_nice_ceil_rounds_to_readable_axis_maxima(self) -> None:
        for value, expected in ((0, 1.0), (0.4, 0.4), (0.42, 0.5), (7, 7.5), (12, 15.0),
                                (23, 25.0), (60, 75.0), (30000, 30000.0)):
            with self.subTest(value=value):
                self.assertEqual(G.nice_ceil(value), expected)


@unittest.skipUnless(has_gtk(), "run.py needs PyGObject")
class TestSettings(unittest.TestCase):
    """Defaults, clamping and the round trip through JSON."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = (G.CONFIG_DIR, G.CONFIG_FILE, G.AUTOSTART_FILE)
        G.CONFIG_DIR = self.tmp.name
        G.CONFIG_FILE = os.path.join(self.tmp.name, "settings.json")
        G.AUTOSTART_FILE = os.path.join(self.tmp.name, "autostart.desktop")

    def tearDown(self) -> None:
        G.CONFIG_DIR, G.CONFIG_FILE, G.AUTOSTART_FILE = self._saved
        self.tmp.cleanup()

    def test_defaults_are_the_documented_ones(self) -> None:
        settings = G.Settings()
        self.assertEqual(settings["lang"], "en")
        self.assertEqual(settings["interval"], 5.0)
        self.assertTrue(settings["start_in_tray"])
        self.assertTrue(settings["auto_open"])
        self.assertEqual(settings["auto_open_level"], G.ORANGE)
        self.assertTrue(settings["notify"])

    def test_notification_defaults_are_quiet(self) -> None:
        """One notification per minute at every level was the bubble wall."""
        settings = G.Settings()
        self.assertEqual(settings["notify_cooldown"], 900.0)
        self.assertEqual(settings["notify_level"], G.RED)

    def test_notify_level_is_clamped_to_a_real_level(self) -> None:
        for value, expected in ((G.YELLOW, G.YELLOW), (G.ORANGE, G.ORANGE),
                                (G.RED, G.RED), (G.GREEN, G.RED), (99, G.RED)):
            with self.subTest(value=value):
                with open(G.CONFIG_FILE, "w") as f:
                    json.dump({"notify_level": value}, f)
                self.assertEqual(G.Settings()["notify_level"], expected)

    def test_cooldown_is_clamped_into_a_sane_range(self) -> None:
        for value, expected in ((1, 60.0), (300, 300.0), (99999, 3600.0),
                                ("soon", 900.0)):
            with self.subTest(value=value):
                with open(G.CONFIG_FILE, "w") as f:
                    json.dump({"notify_cooldown": value}, f)
                self.assertEqual(G.Settings()["notify_cooldown"], expected)

    def test_notifications_are_never_sent_with_urgency_critical(self) -> None:
        """A critical notification does not expire - that is what made the old
        ones impossible to clear."""
        source = open(GUI_PATH).read()
        self.assertNotIn('"-u", "critical"', source)
        self.assertIn('"-u", "normal"', source)
        self.assertIn("NOTIFY_TIMEOUT_MS", source)

    def test_values_survive_a_round_trip(self) -> None:
        settings = G.Settings()
        settings.set("lang", "de")
        settings.set("interval", 10.0)
        self.assertEqual(G.Settings()["lang"], "de")
        self.assertEqual(G.Settings()["interval"], 10.0)

    def test_unknown_keys_are_ignored(self) -> None:
        with open(G.CONFIG_FILE, "w") as f:
            json.dump({"lang": "de", "rm_rf": True}, f)
        settings = G.Settings()
        self.assertEqual(settings["lang"], "de")
        self.assertNotIn("rm_rf", settings.data)

    def test_broken_json_falls_back_to_defaults(self) -> None:
        with open(G.CONFIG_FILE, "w") as f:
            f.write("{not json")
        self.assertEqual(G.Settings()["lang"], "en")

    def test_out_of_range_values_are_clamped(self) -> None:
        with open(G.CONFIG_FILE, "w") as f:
            json.dump({"interval": 9999, "lang": "klingon"}, f)
        settings = G.Settings()
        self.assertEqual(settings["interval"], 60.0)
        self.assertEqual(settings["lang"], "en")

    def test_nonsense_interval_falls_back(self) -> None:
        with open(G.CONFIG_FILE, "w") as f:
            json.dump({"interval": "soon"}, f)
        self.assertEqual(G.Settings()["interval"], 5.0)

    def test_saving_is_atomic_and_leaves_no_temp_file(self) -> None:
        G.Settings().set("notify", False)
        self.assertEqual(os.listdir(self.tmp.name), ["settings.json"])

    def test_autostart_is_derived_from_the_file(self) -> None:
        self.assertFalse(G.Settings.autostart_enabled())
        self.assertTrue(G.Settings.set_autostart(True))
        self.assertTrue(G.Settings.autostart_enabled())
        content = open(G.AUTOSTART_FILE).read()
        self.assertIn("[Desktop Entry]", content)
        self.assertIn("Exec=", content)
        self.assertTrue(G.Settings.set_autostart(False))
        self.assertFalse(G.Settings.autostart_enabled())

    def test_disabling_autostart_twice_is_harmless(self) -> None:
        self.assertTrue(G.Settings.set_autostart(False))
        self.assertTrue(G.Settings.set_autostart(False))

    def test_a_readonly_home_does_not_crash_the_dashboard(self) -> None:
        G.CONFIG_DIR = "/proc/definitely-not-writable"
        G.CONFIG_FILE = os.path.join(G.CONFIG_DIR, "settings.json")
        G.Settings().set("lang", "de")  # must not raise


if __name__ == "__main__":
    unittest.main()
