from datetime import date, datetime, time

import pytest

from ipobot.clock import KST
from ipobot.domain.calendar import KrxCalendar
from ipobot.domain.dates import (
    CalendarScanError,
    add_months,
    attempt_morning,
    base_candidates,
    first_open_on_or_after,
    next_open,
    target_dates,
)


@pytest.fixture
def cal():
    return KrxCalendar()


@pytest.mark.parametrize(
    ("d", "months", "expected"),
    [
        (date(2026, 1, 31), 1, date(2026, 2, 28)),
        (date(2028, 1, 31), 1, date(2028, 2, 29)),  # 윤년
        (date(2026, 3, 31), 1, date(2026, 4, 30)),
        (date(2026, 8, 31), 6, date(2027, 2, 28)),
        (date(2027, 8, 31), 6, date(2028, 2, 29)),
        (date(2026, 12, 31), 12, date(2027, 12, 31)),
        (date(2026, 11, 10), 3, date(2027, 2, 10)),
    ],
)
def test_add_months(d, months, expected):
    assert add_months(d, months) == expected


def test_target_dates():
    assert target_dates(date(2026, 8, 31)) == {
        1: date(2026, 9, 30),
        3: date(2026, 11, 30),
        6: date(2027, 2, 28),
        12: date(2027, 8, 31),
    }


def test_base_candidates_weekday(cal):
    # 목요일 → 수·화·월
    assert base_candidates(date(2026, 11, 12), cal, 3) == [
        date(2026, 11, 11),
        date(2026, 11, 10),
        date(2026, 11, 9),
    ]


def test_base_candidates_monday(cal):
    # 월요일 → 직전 금요일부터
    assert base_candidates(date(2026, 11, 16), cal, 2) == [date(2026, 11, 13), date(2026, 11, 12)]


def test_base_candidates_after_seollal(cal):
    # 2026 설 연휴 2/16~2/18, 직후 첫 개장일 2/19(목) → 그 이전 개장일은 2/13(금)
    assert base_candidates(date(2026, 2, 19), cal, 2) == [date(2026, 2, 13), date(2026, 2, 12)]
    assert base_candidates(date(2026, 2, 20), cal, 2) == [date(2026, 2, 19), date(2026, 2, 13)]


def test_base_candidates_rejects_nonpositive(cal):
    with pytest.raises(ValueError):
        base_candidates(date(2026, 11, 12), cal, 0)


def test_first_open_on_or_after(cal):
    assert first_open_on_or_after(date(2026, 11, 12), cal) == date(2026, 11, 12)
    assert first_open_on_or_after(date(2026, 11, 14), cal) == date(2026, 11, 16)
    assert first_open_on_or_after(date(2026, 2, 14), cal) == date(2026, 2, 19)
    assert first_open_on_or_after(date(2025, 12, 31), cal) == date(2026, 1, 2)


def test_next_open(cal):
    assert next_open(date(2026, 11, 12), cal) == date(2026, 11, 13)
    assert next_open(date(2026, 11, 13), cal) == date(2026, 11, 16)
    assert next_open(date(2026, 4, 30), cal) == date(2026, 5, 4)  # 5/1 근로자의날


def test_scan_limit():
    class AlwaysClosed:
        def is_open(self, d):
            return False

    with pytest.raises(CalendarScanError):
        next_open(date(2026, 1, 1), AlwaysClosed())
    with pytest.raises(CalendarScanError):
        base_candidates(date(2026, 1, 1), AlwaysClosed(), 1)


def test_attempt_morning_friday_judgment_goes_to_monday(cal):
    friday = date(2026, 11, 13)
    at = attempt_morning(next_open(friday, cal), time(9, 0))
    assert at == datetime(2026, 11, 16, 9, 0, tzinfo=KST)
    assert at.tzinfo is KST
