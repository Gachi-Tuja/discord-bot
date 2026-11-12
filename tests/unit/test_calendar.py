from datetime import date

import pytest

from ipobot.domain.calendar import KrxCalendar, year_end_closing_day


@pytest.fixture
def cal():
    return KrxCalendar()


@pytest.mark.parametrize(
    "d",
    [
        date(2026, 11, 14),  # 토
        date(2026, 11, 15),  # 일
        date(2026, 2, 16),  # 설 전날
        date(2026, 2, 17),  # 설
        date(2026, 2, 18),  # 설 다음날
        date(2026, 9, 24),  # 추석 연휴
        date(2026, 9, 25),
        date(2023, 1, 24),  # 설 대체공휴일
        date(2026, 3, 2),  # 삼일절 대체공휴일
        date(2026, 8, 17),  # 광복절 대체공휴일
        date(2026, 5, 1),  # 근로자의날(금)
        date(2025, 5, 1),  # 근로자의날(목)
        date(2025, 12, 31),  # 연말 휴장(수)
        date(2022, 12, 30),  # 12/31 토 → 직전 금
        date(2023, 12, 29),  # 12/31 일 → 직전 금
        date(2028, 12, 29),  # 12/31 일 → 직전 금
        date(2026, 1, 1),
    ],
)
def test_closed_days(cal, d):
    assert cal.is_open(d) is False


@pytest.mark.parametrize(
    "d",
    [
        date(2026, 11, 12),  # 목
        date(2026, 11, 16),  # 월
        date(2026, 2, 19),  # 설 연휴 직후
        date(2026, 12, 30),  # 연말 휴장 전날
        date(2022, 12, 29),
        date(2023, 12, 28),
        date(2026, 4, 30),
    ],
)
def test_open_days(cal, d):
    assert cal.is_open(d) is True


def test_year_end_closing_day():
    assert year_end_closing_day(2025) == date(2025, 12, 31)
    assert year_end_closing_day(2022) == date(2022, 12, 30)
    assert year_end_closing_day(2023) == date(2023, 12, 29)
    assert year_end_closing_day(2028) == date(2028, 12, 29)
    # 12/31이 주말이면 그 날 자체는 주말 규칙으로 휴장
    cal = KrxCalendar()
    assert not cal.is_open(date(2022, 12, 31))
    assert not cal.is_open(date(2023, 12, 31))


def test_override_open_beats_rule_holiday():
    cal = KrxCalendar(overrides={date(2026, 5, 1): True, date(2026, 11, 14): True})
    assert cal.is_open(date(2026, 5, 1))
    assert cal.is_open(date(2026, 11, 14))  # 주말도 override가 이긴다


def test_override_closed_beats_weekday():
    cal = KrxCalendar(overrides={date(2026, 11, 12): False})
    assert not cal.is_open(date(2026, 11, 12))
    assert cal.is_open(date(2026, 11, 13))


def test_krx_closed_day_closes_weekday_and_override_wins():
    d = date(2026, 6, 4)  # 평일
    assert KrxCalendar().is_open(d)
    assert not KrxCalendar(closed_days={d}).is_open(d)
    assert KrxCalendar(overrides={d: True}, closed_days={d}).is_open(d)


def test_add_closed_day_in_memory():
    cal = KrxCalendar()
    d = date(2026, 6, 4)
    cal.add_closed_day(d)
    assert not cal.is_open(d)


class _Source:
    def __init__(self):
        self.ovr: dict[date, bool] = {}
        self.closed: set[date] = set()

    def overrides(self):
        return dict(self.ovr)

    def closed_days(self):
        return set(self.closed)


def test_reload_from_source():
    src = _Source()
    cal = KrxCalendar(source=src)
    d = date(2026, 6, 4)
    assert cal.is_open(d)
    src.closed.add(d)
    assert cal.is_open(d)  # 재로드 전에는 그대로
    cal.reload()
    assert not cal.is_open(d)
    src.ovr[d] = True
    cal.reload()
    assert cal.is_open(d)


def test_reload_from_calendar_repo(tmp_path):
    from datetime import datetime

    from ipobot.clock import KST
    from ipobot.db.connection import open_database
    from ipobot.db.repositories import CalendarRepo

    conn = open_database(tmp_path / "t.sqlite3")
    repo = CalendarRepo(conn)
    repo.add_closed_day(date(2026, 6, 4), date(2026, 6, 5), datetime(2026, 6, 5, 9, tzinfo=KST))
    repo.set_override(date(2026, 5, 1), True)
    cal = KrxCalendar(source=repo)
    assert not cal.is_open(date(2026, 6, 4))
    assert cal.is_open(date(2026, 5, 1))
