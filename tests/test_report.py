import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import report


class PeriodTest(unittest.TestCase):
    def test_current_period_november(self):
        self.assertEqual(
            report.current_period(date(2026, 11, 8)),
            (date(2026, 9, 1), date(2027, 1, 31)),
        )

    def test_current_period_january(self):
        self.assertEqual(
            report.current_period(date(2027, 1, 15)),
            (date(2026, 9, 1), date(2027, 1, 31)),
        )

    def test_current_period_february(self):
        self.assertEqual(
            report.current_period(date(2027, 2, 1)),
            (date(2027, 2, 1), date(2027, 8, 31)),
        )

    def test_current_period_august(self):
        self.assertEqual(
            report.current_period(date(2026, 8, 31)),
            (date(2026, 2, 1), date(2026, 8, 31)),
        )

    def test_current_period_september(self):
        self.assertEqual(
            report.current_period(date(2026, 9, 1)),
            (date(2026, 9, 1), date(2027, 1, 31)),
        )


class DateParsingTest(unittest.TestCase):
    def test_dot_dates(self):
        self.assertEqual(
            report.extract_dates("02.11.2026 -- 03.11.2026"),
            [date(2026, 11, 2), date(2026, 11, 3)],
        )

    def test_word_dates(self):
        self.assertEqual(
            report.extract_dates("28 мая 2026 - 15 июля 2026"),
            [date(2026, 5, 28), date(2026, 7, 15)],
        )

    def test_normalize_course_uses_issue_date(self):
        self.assertEqual(
            report.normalize_dates("28.05.2026 - 15.07.2026; дата выдачи 17.08.2026", "курс"),
            "17.08.2026",
        )

    def test_normalize_competition_keeps_range(self):
        self.assertEqual(
            report.normalize_dates("12.05.2025 - 15.05.2025", "конкурс"),
            "12.05.2025 – 15.05.2025",
        )

    def test_document_in_period_uses_end_date(self):
        start, end = date(2026, 9, 1), date(2027, 1, 31)
        self.assertTrue(
            report.document_in_period("02.11.2026 – 03.11.2026", "конкурс", start, end)
        )
        self.assertFalse(
            report.document_in_period("02.11.2025 – 03.11.2025", "конкурс", start, end)
        )

    def test_year_only_fallback(self):
        start, end = date(2026, 9, 1), date(2027, 1, 31)
        self.assertTrue(report.document_in_period("2026", "конкурс", start, end))
        self.assertFalse(report.document_in_period("2025", "конкурс", start, end))


if __name__ == "__main__":
    unittest.main()
