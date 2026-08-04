"""Unit tests for the drawing widgets in run.py.

Charts are rendered offscreen and only their behaviour is asserted - scaling,
scrolling, hover mapping, table updates. Whether a line is beautiful is not
testable; whether it crashes on an empty series, a single sample or a value above
the axis maximum very much is.
"""

from __future__ import annotations

import time
import unittest

from tests.fg_test_utils import gui_testable, has_gtk, load_gui, pump

if has_gtk():
    G = load_gui()
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk


def offscreen(widget, width=600, height=140):
    """Renders a widget offscreen and returns its pixbuf."""
    window = Gtk.OffscreenWindow()
    window.add(widget)
    window.set_size_request(width, height)
    window.show_all()
    pump()
    return window.get_pixbuf()


@unittest.skipUnless(gui_testable(), "needs GTK and a display")
class TestTimeChart(unittest.TestCase):
    """Axis scaling, capacity and the hover layer."""

    def chart(self, **kwargs):
        return G.TimeChart([("a", 0), ("b", 1)], **kwargs)

    def test_empty_chart_draws_without_data(self) -> None:
        self.assertIsNotNone(offscreen(self.chart()))

    def test_single_sample_draws_without_data(self) -> None:
        chart = self.chart()
        chart.push(time.time(), (5.0, 6.0))
        self.assertIsNotNone(offscreen(chart))

    def test_axis_never_falls_below_the_minimum(self) -> None:
        chart = self.chart(min_ymax=10)
        chart.push(time.time(), (0.1, 0.2))
        self.assertGreaterEqual(chart._ymax(), 10)

    def test_axis_grows_with_the_data(self) -> None:
        chart = self.chart(min_ymax=10)
        chart.push(time.time(), (0.0, 0.0))
        chart.push(time.time(), (73.0, 0.0))
        self.assertGreaterEqual(chart._ymax(), 73)

    def test_axis_leaves_room_above_the_threshold(self) -> None:
        """The kill line must never sit on the top edge, or it reads as 'reached'."""
        chart = self.chart(min_ymax=10, threshold=20)
        chart.push(time.time(), (1.0, 1.0))
        self.assertGreater(chart._ymax(), 20)

    def test_capacity_is_a_hard_window(self) -> None:
        chart = self.chart(capacity=10)
        now = time.time()
        for i in range(25):
            chart.push(now + i, (float(i), 0.0))
        self.assertEqual(len(chart.times), 10)
        self.assertEqual(chart.data[0][-1], 24.0)

    def test_line_grows_from_the_left_before_it_scrolls(self) -> None:
        """A half-filled chart must not stretch its samples across the frame -
        that would fake a longer history than exists."""
        chart = self.chart(capacity=100)
        now = time.time()
        for i in range(5):
            chart.push(now + i, (1.0, 1.0))
        offscreen(chart)
        x0, _y0, pw, _ph = chart._plot()
        step = pw / 99
        self.assertAlmostEqual(chart._x(0, x0, pw), x0, delta=0.01)
        self.assertAlmostEqual(chart._x(4, x0, pw), x0 + 4 * step, delta=0.01)

    def test_time_mode_maps_by_timestamp(self) -> None:
        """In the recording the distance between samples carries meaning, so a
        gap has to occupy width."""
        chart = G.TimeChart([("a", 0)], x_mode="time", capacity=1000)
        t0 = time.time()
        chart.set_data([t0, t0 + 10, t0 + 610], [[1.0, 2.0, 3.0]],
                       bands=[(t0 + 10, t0 + 610)])
        offscreen(chart)
        x0, _y0, pw, _ph = chart._plot()
        middle = chart._x(1, x0, pw)
        self.assertLess(middle - x0, pw * 0.05)          # 10 s of 610
        self.assertAlmostEqual(chart._x(2, x0, pw), x0 + pw, delta=0.01)

    def test_outage_bands_are_drawn(self) -> None:
        chart = G.TimeChart([("a", 0)], x_mode="time", capacity=1000)
        t0 = time.time()
        chart.set_data([t0, t0 + 600], [[1.0, 2.0]], bands=[(t0 + 100, t0 + 200)])
        self.assertEqual(len(chart.bands), 1)
        self.assertIsNotNone(offscreen(chart))

    def test_values_above_the_axis_are_clamped_not_dropped(self) -> None:
        chart = self.chart(min_ymax=10)
        chart.push(time.time(), (5.0, 5.0))
        chart.data[0][-1] = 1e9  # bypass the autoscale on purpose
        self.assertIsNotNone(offscreen(chart))

    def test_hover_picks_the_nearest_sample(self) -> None:
        chart = self.chart(capacity=20)
        now = time.time()
        for i in range(20):
            chart.push(now + i, (float(i), 0.0))
        offscreen(chart)
        x0, _y0, pw, _ph = chart._plot()
        for index in (0, 7, 19):
            with self.subTest(index=index):
                self.assertEqual(chart._index_at(chart._x(index, x0, pw)), index)

    def test_hover_far_away_selects_nothing(self) -> None:
        chart = self.chart(capacity=20)
        chart.push(time.time(), (1.0, 1.0))
        offscreen(chart)
        self.assertIsNone(chart._index_at(10_000))

    def test_hover_renders_the_tooltip(self) -> None:
        chart = self.chart(capacity=20)
        now = time.time()
        for i in range(20):
            chart.push(now + i, (float(i), float(i) / 2))
        chart.hover = 10
        self.assertIsNotNone(offscreen(chart))

    def test_hover_on_an_empty_chart_is_harmless(self) -> None:
        chart = self.chart()
        chart.hover = 5
        self.assertIsNotNone(offscreen(chart))

    def test_single_series_chart_renders_its_value(self) -> None:
        chart = G.TimeChart([("only", 0)], unit="kB/s", digits=0)
        now = time.time()
        for i in range(30):
            chart.push(now + i, (i * 100.0,))
        self.assertIsNotNone(offscreen(chart))


@unittest.skipUnless(gui_testable(), "needs GTK and a display")
class TestMeter(unittest.TestCase):
    """The usage bar clamps and never loses its marker."""

    def test_fraction_is_clamped(self) -> None:
        meter = G.Meter(marker=0.85)
        for value, expected in ((-1, 0.0), (0.5, 0.5), (2, 1.0)):
            meter.set(value, "good")
            self.assertEqual(meter.frac, expected)

    def test_every_status_renders(self) -> None:
        for status in ("good", "warning", "serious", "critical"):
            with self.subTest(status=status):
                meter = G.Meter(marker=0.85)
                meter.set(0.6, status)
                self.assertIsNotNone(offscreen(meter, 300, 20))

    def test_zero_and_full_render(self) -> None:
        meter = G.Meter()
        meter.set(0.0, "good")
        self.assertIsNotNone(offscreen(meter, 300, 20))
        meter.set(1.0, "critical")
        self.assertIsNotNone(offscreen(meter, 300, 20))


@unittest.skipUnless(gui_testable(), "needs GTK and a display")
class TestProcTable(unittest.TestCase):
    """The process list keeps its rows stable and hands the right pid to the kill."""

    def build(self):
        killed = []
        table = G.ProcTable(
            [("Process", 0.0, True), ("RSS", 1.0, False)], 5,
            lambda pid, comm: killed.append((pid, comm)))
        return table, killed

    def rows(self, count=3, offset=0):
        from gi.repository import Pango
        return [(f"proc{i}", f"{i} MB", Pango.Weight.NORMAL, 1000 + i + offset,
                 f"proc{i}") for i in range(count)]

    def test_fill_adds_the_icon_column(self) -> None:
        table, _killed = self.build()
        table.fill(self.rows(3))
        self.assertEqual(len(table.store), 3)
        self.assertEqual(table.store[0][table.ncols], G.KILL_ICON)

    def test_same_row_count_updates_in_place(self) -> None:
        """Clearing the model on every tick would drop the selection and the
        scroll position - the list is refreshed once per second."""
        table, _killed = self.build()
        table.fill(self.rows(3))
        first_iter = table.store.get_iter_first()
        table.fill(self.rows(3, offset=100))
        self.assertEqual(len(table.store), 3)
        self.assertEqual(table.store.get_path(first_iter).to_string(), "0")
        self.assertEqual(table.store[0][table.ncols + 2], 1100)

    def test_changed_row_count_rebuilds(self) -> None:
        table, _killed = self.build()
        table.fill(self.rows(3))
        table.fill(self.rows(1))
        self.assertEqual(len(table.store), 1)

    def test_empty_fill_clears(self) -> None:
        table, _killed = self.build()
        table.fill(self.rows(2))
        table.fill([])
        self.assertEqual(len(table.store), 0)

    def test_pid_and_comm_travel_with_the_row(self) -> None:
        table, killed = self.build()
        table.fill(self.rows(2))
        it = table.store.get_iter_first()
        table.on_kill(table.store.get_value(it, table.ncols + 2),
                      table.store.get_value(it, table.ncols + 3))
        self.assertEqual(killed, [(1000, "proc0")])


@unittest.skipUnless(gui_testable(), "needs GTK and a display")
class TestTheme(unittest.TestCase):
    """Light and dark are two selected palettes, not an automatic inversion."""

    def test_both_palettes_define_the_same_roles(self) -> None:
        self.assertEqual(set(G.LIGHT), set(G.DARK))
        self.assertEqual(len(G.LIGHT["series"]), len(G.DARK["series"]))

    def test_status_colours_are_shared_by_both_modes(self) -> None:
        self.assertEqual(set(G.STATUS), {"good", "warning", "serious", "critical"})

    def test_apply_theme_loads_css_for_both_modes(self) -> None:
        window = Gtk.OffscreenWindow()
        original = G.detect_dark
        try:
            for dark in (True, False):
                G.detect_dark = lambda _w, value=dark: value
                G.apply_theme(window)
                self.assertEqual(G.PAL["surface"],
                                 G.DARK["surface"] if dark else G.LIGHT["surface"])
        finally:
            G.detect_dark = original
            G.apply_theme(window)

    def test_rgba_parses_hex(self) -> None:
        self.assertEqual(G.rgba("#ffffff"), (1.0, 1.0, 1.0, 1.0))
        self.assertEqual(G.rgba("#000000", 0.5)[3], 0.5)


if __name__ == "__main__":
    unittest.main()
