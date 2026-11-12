"""KRX 거래 캘린더(§8 용어 「캘린더 개장일」).

판단 우선순위
1. market_calendar_overrides
2. krx_closed_days (KRX 관측 휴장 → closed)
3. 규칙: 주말, holidays.KR 공휴일, 근로자의날(5/1), 연말 휴장일(12/31, 주말이면 직전 금요일)

캘린더는 가장 이른 시도 시각만 정한다. 판정 거래일은 매 시도마다 KRX 관측으로 재도출한다.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, timedelta
from typing import Protocol

import holidays


class TradingCalendar(Protocol):
    def is_open(self, d: date) -> bool: ...


class CalendarSource(Protocol):
    def overrides(self) -> dict[date, bool]: ...

    def closed_days(self) -> set[date]: ...


def year_end_closing_day(year: int) -> date:
    """연말 휴장일: 12/31. 토·일이면 직전 금요일."""
    d = date(year, 12, 31)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def is_labor_day(d: date) -> bool:
    return d.month == 5 and d.day == 1


class KrxCalendar:
    def __init__(
        self,
        overrides: Mapping[date, bool] | None = None,
        closed_days: Iterable[date] | None = None,
        *,
        source: CalendarSource | None = None,
    ) -> None:
        self._source = source
        self._kr_holidays = holidays.KR()
        self._overrides: dict[date, bool] = dict(overrides or {})
        self._closed: set[date] = set(closed_days or ())
        if source is not None:
            self.reload()

    def reload(self) -> None:
        """DB 계층(override, krx_closed_days)을 다시 읽는다. source가 없으면 아무것도 하지 않는다."""
        if self._source is None:
            return
        self._overrides = dict(self._source.overrides())
        self._closed = set(self._source.closed_days())

    def add_closed_day(self, d: date) -> None:
        """방금 기록한 관측 휴장을 메모리에도 반영한다."""
        self._closed.add(d)

    def rule_is_open(self, d: date) -> bool:
        if d.weekday() >= 5:
            return False
        if d in self._kr_holidays or is_labor_day(d):
            return False
        return d != year_end_closing_day(d.year)

    def is_open(self, d: date) -> bool:
        override = self._overrides.get(d)
        if override is not None:
            return override
        if d in self._closed:
            return False
        return self.rule_is_open(d)
