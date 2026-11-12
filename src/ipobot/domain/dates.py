"""날짜 계산(R2·R8·R1). 모든 함수는 순수하며 캘린더를 주입받는다."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from dateutil.relativedelta import relativedelta

from ipobot.clock import KST
from ipobot.domain.calendar import TradingCalendar

HORIZONS: tuple[int, ...] = (1, 3, 6, 12)
# 연속 휴장은 길어야 열흘 남짓이다. 이 범위를 넘으면 캘린더 설정 오류로 본다.
MAX_SCAN_DAYS = 60


class CalendarScanError(RuntimeError):
    pass


def add_months(d: date, months: int) -> date:
    """달력상 n개월 뒤. 해당 월에 같은 날이 없으면 그 달 말일(1/31+1M=2/28 또는 2/29)."""
    return d + relativedelta(months=months)


def target_dates(base_date: date) -> dict[int, date]:
    return {h: add_months(base_date, h) for h in HORIZONS}


def first_open_on_or_after(d: date, cal: TradingCalendar) -> date:
    for i in range(MAX_SCAN_DAYS):
        candidate = d + timedelta(days=i)
        if cal.is_open(candidate):
            return candidate
    raise CalendarScanError(f"{d} 이후 {MAX_SCAN_DAYS}일 안에 개장일이 없습니다")


def next_open(d: date, cal: TradingCalendar) -> date:
    """d보다 뒤의 첫 개장일."""
    return first_open_on_or_after(d + timedelta(days=1), cal)


def base_candidates(today: date, cal: TradingCalendar, lookback: int) -> list[date]:
    """today 미만의 캘린더 개장일을 최신부터 lookback개."""
    if lookback < 1:
        raise ValueError("lookback은 1 이상이어야 합니다")
    found: list[date] = []
    d = today
    for _ in range(MAX_SCAN_DAYS + lookback * 7):
        d -= timedelta(days=1)
        if cal.is_open(d):
            found.append(d)
            if len(found) == lookback:
                return found
    raise CalendarScanError(f"{today} 이전에서 개장일 {lookback}개를 찾지 못했습니다")


def attempt_morning(d: date, judge_time: time) -> datetime:
    """d일 judge_time(KST). 판정 시도 시각으로 쓴다(예: attempt_morning(next_open(금), 09:00) → 월 09:00)."""
    return datetime.combine(d, judge_time, tzinfo=KST)
