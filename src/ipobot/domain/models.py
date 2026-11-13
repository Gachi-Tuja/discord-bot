"""도메인 값 객체. I/O 없이 정책·서비스·포맷팅이 공유한다."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

Market = Literal["KOSPI", "KOSDAQ", "KONEX"]
MARKETS: tuple[Market, ...] = ("KOSPI", "KOSDAQ", "KONEX")

NotTradedReason = Literal["halted", "absent"]
FetchErrorKind = Literal["auth", "transient", "data_anomaly"]


@dataclass(frozen=True)
class DailyQuote:
    """KRX 일별 매매정보 한 행(비수정 종가)."""

    trade_date: date
    market: Market
    stock_code: str
    corp_name: str
    close_price: int
    open_price: int
    trade_volume: int
    market_cap: int
    listed_shares: int


# --- DayObs: 한 종목·한 거래일의 관측(R9) ---


@dataclass(frozen=True)
class Traded:
    quote: DailyQuote


@dataclass(frozen=True)
class NotTraded:
    reason: NotTradedReason


@dataclass(frozen=True)
class AllEmpty:
    """세 시장 모두 빈 응답(휴장 또는 아직 미공개)."""


@dataclass(frozen=True)
class Unpublished:
    """코드를 아직 못 찾았고 미공개 시장이 하나 이상(나머지 시장은 공개)."""

    missing_markets: frozenset[Market]

    def __post_init__(self) -> None:
        if not self.missing_markets:
            raise ValueError("Unpublished에는 미공개 시장이 하나 이상 있어야 합니다")
        if len(self.missing_markets) >= len(MARKETS):
            raise ValueError("세 시장이 모두 미공개면 AllEmpty입니다")


@dataclass(frozen=True)
class FetchError:
    kind: FetchErrorKind


DayObs = Traded | NotTraded | AllEmpty | Unpublished | FetchError


@dataclass(frozen=True)
class UnavailStreak:
    """관측 미가용 연속 구간(judgments.unavailable_since/last_day/days)."""

    since: date | None = None
    last_day: date | None = None
    days: int = 0

    def __post_init__(self) -> None:
        if self.days < 0:
            raise ValueError("days는 0 이상이어야 합니다")
        if (self.days == 0) != (self.since is None) or (self.since is None) != (self.last_day is None):
            raise ValueError(f"streak 불변식 위반: {self}")
        if self.since is not None and self.last_day is not None and self.last_day < self.since:
            raise ValueError(f"last_day는 since 이후여야 합니다: {self}")

    @property
    def is_empty(self) -> bool:
        return self.days == 0


EMPTY_STREAK = UnavailStreak()
