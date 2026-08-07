import unittest
from datetime import datetime

import pytz

from trendradar.core.weekly import previous_natural_week
from trendradar.utils.time import parse_iso_datetime


class NaturalWeekWindowTests(unittest.TestCase):
    def test_previous_week_is_monday_to_monday_in_shanghai(self):
        tz = pytz.timezone("Asia/Shanghai")
        window = previous_natural_week(
            tz.localize(datetime(2026, 8, 10, 10, 0)),
            "Asia/Shanghai",
        )

        self.assertEqual(window.start.isoformat(), "2026-08-03T00:00:00+08:00")
        self.assertEqual(window.end.isoformat(), "2026-08-10T00:00:00+08:00")
        self.assertEqual(window.label, "2026-08-03—2026-08-09")
        self.assertEqual(
            window.storage_dates,
            [
                "2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06",
                "2026-08-07", "2026-08-08", "2026-08-09", "2026-08-10",
            ],
        )

    def test_window_is_half_open_across_year_boundary(self):
        tz = pytz.timezone("Asia/Shanghai")
        window = previous_natural_week(
            tz.localize(datetime(2027, 1, 4, 10, 0)),
            "Asia/Shanghai",
        )

        self.assertTrue(window.contains("2026-12-28T00:00:00+08:00"))
        self.assertTrue(window.contains("2027-01-03T23:59:59+08:00"))
        self.assertFalse(window.contains("2027-01-04T00:00:00+08:00"))
        self.assertFalse(window.contains(""))
        self.assertFalse(window.contains("invalid"))

    def test_naive_iso_time_keeps_existing_utc_assumption(self):
        parsed = parse_iso_datetime("2026-08-02T16:00:00", "Asia/Shanghai")
        self.assertEqual(parsed.isoformat(), "2026-08-03T00:00:00+08:00")
