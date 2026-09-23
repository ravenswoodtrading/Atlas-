"""VA report speed-ups (2026-09-21): the cached row-value and date parsing must give EXACTLY what the old per-call versions
gave (they are kept here as the yardstick), and the Lead Sheet read must be served from a background-refreshed cache.
No network: open_sheet is faked."""
import random
import time
import unittest
from datetime import date, datetime
from unittest.mock import patch

from app.routes import reports


def old_value(row, *names):
    normalise = lambda value: " ".join(str(value).strip().lower().split())
    wanted = {normalise(name) for name in names}
    return next((value for key, value in row.items() if normalise(key) in wanted), None)


def old_date_candidates(row):
    text = str(old_value(row, "date", "date submitted", "date added") or "").strip()
    candidates = []
    for pattern in ("%d %b %y", "%d %b %Y", "%d/%m/%Y", "%m/%d/%Y", "%d/%m/%y", "%m/%d/%y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S"):
        try:
            parsed = datetime.strptime(text, pattern).date()
            if parsed not in candidates:
                candidates.append(parsed)
        except ValueError:
            pass
    return candidates


HEADERS = ["Date", "Image", "Product Name", "ASIN", "Sourcing Method", " Sourcing  method  used ", "Client Rating",
           "Expected Profit", "Purchased Qty", "Date Last Added", "date", "DATE ADDED", "Price drop (>5%)", "Actual CoG \n(unit)"]
DATES = ["1 Jul 26", "01 Jul 2026", "3/7/2026", "7/3/2026", "12/11/26", "2026-07-03", "03-07-2026", "3/7/2026 10:15:00",
         "", "New ASIN", "31/12/2026", "13/13/2026", "1 Feb 27", "  4 Aug 26  ", "Sat", "0", None]
WORDS = ["OA", "EU", "UK", "A2A", "Online Arbitrage", "yes", "No", "good", "Avoid", "12.50", "£3.20", "", "x", None, 5, 2.5]


def random_row(rng):
    keys = rng.sample(HEADERS, rng.randint(3, len(HEADERS)))
    if rng.random() < 0.2:
        keys.append(rng.choice([3, None, "  "]))                     # awkward, non-string keys
    return {k: rng.choice(DATES if "ate" in str(k) else WORDS) for k in keys}


NAME_SETS = [("date", "date submitted", "date added"), ("sourcing method", "source method", "method"),
             ("client rating", "client_rating", "rating"), ("Price drop (>5%)",), ("purchased qty", "purchased quantity"),
             ("actual cog", "cog (unit)", "cost price", "cog"), ("expected profit", "profit", "net profit"),
             ("sourcing method used",), ("nothing like this",)]


class SameAnswersAsTheOldCode(unittest.TestCase):
    def test_value_and_dates_match_on_thousands_of_random_rows(self):
        rng = random.Random(7)
        for _ in range(3000):
            row = random_row(rng)
            for names in NAME_SETS:
                self.assertEqual(reports._value(row, *names), old_value(row, *names), (row, names))
            self.assertEqual(reports._date_candidates(row), old_date_candidates(row), row)

    def test_the_first_matching_column_in_row_order_still_wins(self):
        row = {"Date Added": "1 Jul 26", "Date": "2 Jul 26"}          # both match; the first key in the row decides
        self.assertEqual(reports._value(row, "date", "date added"), "1 Jul 26")
        self.assertEqual(reports._date_candidates(row), [date(2026, 7, 1)])

    def test_ambiguous_dates_still_give_every_reading_in_pattern_order(self):
        self.assertEqual(reports._date_candidates({"Date": "3/7/2026"}), [date(2026, 7, 3), date(2026, 3, 7)])
        self.assertEqual(reports._date_candidates({"Date": "nonsense"}), [])
        self.assertEqual(reports._date_candidates({}), [])

    def test_callers_can_not_corrupt_the_cache_by_mutating_the_result(self):
        first = reports._date_candidates({"Date": "3/7/2026"})
        first.clear()
        self.assertEqual(len(reports._date_candidates({"Date": "3/7/2026"})), 2)


class FakeWorkbook:
    reads = 0
    values = [["Date", "ASIN", "Sourcing Method"], ["1 Jul 26", "B0AAAAAAA1", "EU"], ["", "", ""], ["2 Jul 26", "B0AAAAAAA2", "OA"]]
    fail = False

    def worksheet(self, tab):
        return self

    def get_all_values(self):
        FakeWorkbook.reads += 1
        if FakeWorkbook.fail:
            raise RuntimeError("Google unavailable")
        return [list(r) for r in FakeWorkbook.values]


class SheetCacheTests(unittest.TestCase):
    def setUp(self):
        FakeWorkbook.reads, FakeWorkbook.fail = 0, False
        reports._VA_SHEET_CACHE.invalidate()
        p = patch.object(reports, "open_sheet", lambda url: FakeWorkbook())
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(reports._VA_SHEET_CACHE.invalidate)

    def test_rows_are_built_from_the_sheet_and_blank_rows_dropped(self):
        rows = reports._sheet_rows()
        self.assertEqual(rows, [{"Date": "1 Jul 26", "ASIN": "B0AAAAAAA1", "Sourcing Method": "EU"},
                                {"Date": "2 Jul 26", "ASIN": "B0AAAAAAA2", "Sourcing Method": "OA"}])

    def test_repeat_calls_do_not_read_the_sheet_again(self):
        for _ in range(5):
            reports._sheet_rows()
        self.assertEqual(FakeWorkbook.reads, 1)

    def test_an_empty_sheet_gives_no_rows(self):
        FakeWorkbook.values = []
        try:
            self.assertEqual(reports._sheet_rows(), [])
        finally:
            FakeWorkbook.values = [["Date", "ASIN", "Sourcing Method"], ["1 Jul 26", "B0AAAAAAA1", "EU"], ["", "", ""], ["2 Jul 26", "B0AAAAAAA2", "OA"]]

    def test_fresh_forces_a_new_read(self):
        reports._sheet_rows()
        reports._sheet_rows(fresh=True)
        self.assertEqual(FakeWorkbook.reads, 2)

    def test_a_stale_read_is_served_at_once_while_the_sheet_is_re_read_behind_it(self):
        reports._sheet_rows()
        reports._VA_SHEET_CACHE._entries["rows"][1] -= 10_000            # make it stale
        FakeWorkbook.values = FakeWorkbook.values + [["3 Jul 26", "B0AAAAAAA3", "UK"]]
        try:
            served = reports._sheet_rows()
            self.assertEqual(len(served), 2)                             # old rows, immediately
            end = time.time() + 5
            while reports._VA_SHEET_CACHE.refreshing() and time.time() < end:
                time.sleep(0.01)
            self.assertEqual(len(reports._sheet_rows()), 3)              # the refreshed rows next time
        finally:
            FakeWorkbook.values = FakeWorkbook.values[:-1]

    def test_the_first_read_failing_still_raises_so_the_page_shows_its_sheet_error(self):
        FakeWorkbook.fail = True
        with self.assertRaises(RuntimeError):
            reports._sheet_rows()

    def test_a_refresh_failing_keeps_serving_the_last_good_rows(self):
        reports._sheet_rows()
        reports._VA_SHEET_CACHE._entries["rows"][1] -= 10_000
        FakeWorkbook.fail = True
        self.assertEqual(len(reports._sheet_rows()), 2)
        end = time.time() + 5
        while reports._VA_SHEET_CACHE.refreshing() and time.time() < end:
            time.sleep(0.01)
        self.assertEqual(len(reports._sheet_rows()), 2)


if __name__ == "__main__":
    unittest.main()
