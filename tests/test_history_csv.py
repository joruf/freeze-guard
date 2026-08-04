"""Unit tests for the recording: the CSV format and its forensic reading.

A gap in the timeline IS the freeze - that is the one inference the whole
post-mortem rests on, so gap detection gets the most attention here.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest

from tests.fg_test_utils import CSV_HEADER, csv_row, has_gtk, load_gui, load_monitor, write_history

M = load_monitor()
if has_gtk():
    G = load_gui()


class TestCsvFormat(unittest.TestCase):
    """The header is a data format shared by recorder and dashboard."""

    def test_column_order_is_the_documented_one(self) -> None:
        self.assertEqual(",".join(M.CSV_COLUMNS), CSV_HEADER)

    def test_top_is_the_last_column(self) -> None:
        """The reader splits with a maxsplit, which only works while the free-text
        column sits at the end."""
        self.assertEqual(M.CSV_COLUMNS[-1], "top")

    def test_time_is_the_first_column(self) -> None:
        self.assertEqual(M.CSV_COLUMNS[0], "t")


@unittest.skipUnless(has_gtk(), "the reader lives in run.py")
class TestHistoryReader(unittest.TestCase):
    """Parsing, gap detection and the three failure modes."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "history.csv")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_missing_file_is_reported_as_missing(self) -> None:
        history = G.History(os.path.join(self.tmp.name, "nope.csv"), 48)
        self.assertEqual(history.error, "missing")
        self.assertEqual(history.rows, [])

    def test_foreign_format_is_rejected(self) -> None:
        with open(self.path, "w") as f:
            f.write("date,value\n1,2\n")
        self.assertEqual(G.History(self.path, 48).error, "format")

    def test_plain_recording_is_read(self) -> None:
        first, last, _g0, _g1 = write_history(self.path, samples=30)
        history = G.History(self.path, 48)
        self.assertIsNone(history.error)
        self.assertEqual(len(history.rows), 30)
        self.assertAlmostEqual(history.times()[0], first, delta=1)
        self.assertAlmostEqual(history.times()[-1], last, delta=1)
        self.assertEqual(history.gaps, [])
        self.assertEqual(history.alarms, [])

    def test_a_gap_longer_than_a_minute_is_an_outage(self) -> None:
        _f, _l, gap_start, gap_end = write_history(
            self.path, samples=40, gap_after=20, gap_seconds=600)
        history = G.History(self.path, 48)
        self.assertEqual(len(history.gaps), 1)
        found_start, found_end = history.gaps[0]
        self.assertAlmostEqual(found_start, gap_start, delta=1)
        self.assertAlmostEqual(found_end, gap_end, delta=6)

    def test_a_short_hiccup_is_not_an_outage(self) -> None:
        """Two skipped samples are scheduling noise, not a dead machine."""
        write_history(self.path, samples=30, gap_after=10, gap_seconds=45)
        self.assertEqual(G.History(self.path, 48).gaps, [])

    def test_rows_before_the_outage_are_the_evidence(self) -> None:
        _f, _l, gap_start, _gap_end = write_history(
            self.path, samples=40, gap_after=20, gap_seconds=600, alarm_from=16)
        history = G.History(self.path, 48)
        tail = history.rows_before(gap_start)
        self.assertTrue(tail)
        self.assertLessEqual(len(tail), 8)
        self.assertTrue(all(row[0] <= gap_start for row in tail))
        self.assertEqual(tail[-1][history.idx["level"]], "KRITISCH")

    def test_alarms_are_every_row_above_ok(self) -> None:
        write_history(self.path, samples=30, alarm_from=25)
        history = G.History(self.path, 48)
        self.assertEqual(len(history.alarms), 5)

    def test_the_time_window_is_honoured(self) -> None:
        now = time.time()
        with open(self.path, "w") as f:
            f.write(CSV_HEADER + "\n")
            f.write(csv_row(now - 40 * 3600) + "\n")   # older than 24 h
            f.write(csv_row(now - 60) + "\n")
        self.assertEqual(len(G.History(self.path, 24).rows), 1)
        self.assertEqual(len(G.History(self.path, 48).rows), 2)

    def test_truncated_and_garbled_lines_are_skipped(self) -> None:
        write_history(self.path, samples=5)
        with open(self.path, "a") as f:
            f.write("not,enough,columns\n")
            f.write("no-timestamp,OK,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,\"x\"\n")
        history = G.History(self.path, 48)
        self.assertIsNone(history.error)
        self.assertEqual(len(history.rows), 5)

    def test_non_numeric_values_do_not_break_the_charts(self) -> None:
        write_history(self.path, samples=3)
        history = G.History(self.path, 48)
        history.rows[1][history.idx["mem_avail_pct"]] = "n/a"
        column = history.column("mem_avail_pct")
        self.assertEqual(len(column), 3)
        self.assertEqual(column[1], 0.0)

    def test_unknown_column_yields_an_empty_series(self) -> None:
        write_history(self.path, samples=3)
        self.assertEqual(G.History(self.path, 48).column("nope"), [])

    def test_unreadable_file_is_reported_as_permission(self) -> None:
        if os.getuid() == 0:
            self.skipTest("root reads everything")
        write_history(self.path, samples=3)
        os.chmod(self.path, 0o000)
        try:
            self.assertEqual(G.History(self.path, 48).error, "permission")
        finally:
            os.chmod(self.path, 0o600)

    def test_empty_recording_has_no_rows_and_no_error(self) -> None:
        with open(self.path, "w") as f:
            f.write(CSV_HEADER + "\n")
        history = G.History(self.path, 48)
        self.assertIsNone(history.error)
        self.assertEqual(history.rows, [])


class TestRecorderWrites(unittest.TestCase):
    """The recorder writes the header once and one row per sample."""

    def test_row_layout_matches_the_reader(self) -> None:
        row = csv_row(time.time(), level="WARNUNG", avail=12.5, full10=7.5,
                      swapout=1234)
        fields = row.split(",", len(M.CSV_COLUMNS) - 1)
        self.assertEqual(len(fields), len(M.CSV_COLUMNS))
        self.assertEqual(fields[M.CSV_COLUMNS.index("level")], "WARNUNG")
        self.assertEqual(fields[M.CSV_COLUMNS.index("mem_avail_pct")], "12.5")
        self.assertTrue(fields[-1].startswith('"'))


if __name__ == "__main__":
    unittest.main()
