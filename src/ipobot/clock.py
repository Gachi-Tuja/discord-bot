"""시계 추상화. 서비스는 Clock을 주입받아 테스트에서 시간을 고정한다."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

from ipobot.config import TIMEZONE_NAME

KST = ZoneInfo(TIMEZONE_NAME)


class Clock(Protocol):
    def now(self) -> datetime:
        """시간대가 붙은 현재 시각(KST)."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(KST)


def today_kst(clock: Clock) -> date:
    return clock.now().astimezone(KST).date()


def utc_iso(dt: datetime) -> str:
    """DB 저장용 ISO-8601 UTC 문자열."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime은 저장할 수 없습니다")
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
