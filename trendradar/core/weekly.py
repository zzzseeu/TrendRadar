"""自然周时间窗口。"""

from dataclasses import dataclass
from datetime import datetime, timedelta

import pytz

from trendradar.utils.time import parse_iso_datetime


@dataclass(frozen=True)
class NaturalWeekWindow:
    start: datetime
    end: datetime
    timezone: str

    @property
    def label(self) -> str:
        return f"{self.start:%Y-%m-%d}—{self.end - timedelta(days=1):%Y-%m-%d}"

    @property
    def storage_dates(self) -> list[str]:
        return [
            (self.start + timedelta(days=offset)).strftime("%Y-%m-%d")
            for offset in range(8)
        ]

    def contains(self, published_at: str) -> bool:
        parsed = parse_iso_datetime(published_at, self.timezone)
        return parsed is not None and self.start <= parsed < self.end


def previous_natural_week(now: datetime, timezone: str) -> NaturalWeekWindow:
    tz = pytz.timezone(timezone)
    local_now = now.astimezone(tz)
    monday_date = (local_now - timedelta(days=local_now.weekday())).date()
    end = tz.localize(datetime.combine(monday_date, datetime.min.time()))
    return NaturalWeekWindow(end - timedelta(days=7), end, timezone)
