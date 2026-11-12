from datetime import datetime, timezone

import pytest

from ipobot.clock import KST, SystemClock, today_kst, utc_iso


class _Fixed:
    def __init__(self, dt):
        self.dt = dt

    def now(self):
        return self.dt


def test_system_clock_is_kst_aware():
    now = SystemClock().now()
    assert now.utcoffset().total_seconds() == 9 * 3600


def test_today_kst_converts_from_utc():
    # 2026-11-11 15:30 UTC == 2026-11-12 00:30 KST
    clock = _Fixed(datetime(2026, 11, 11, 15, 30, tzinfo=timezone.utc))
    assert today_kst(clock).isoformat() == "2026-11-12"


def test_utc_iso():
    assert utc_iso(datetime(2026, 11, 12, 9, 0, tzinfo=KST)) == "2026-11-12T00:00:00+00:00"
    with pytest.raises(ValueError):
        utc_iso(datetime(2026, 11, 12, 9, 0))
