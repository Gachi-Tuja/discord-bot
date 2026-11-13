"""도메인 값 객체. I/O 없이 정책·서비스·포맷팅이 공유한다."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
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


@dataclass(frozen=True)
class RowAnomaly:
    """KRX 응답에서 필수 필드(TDD_CLSPRC·ACC_TRDVOL·MKTCAP·LIST_SHRS·ISU_CD)가 결측·비숫자인 행.

    읽을 수 있었던 값은 채운다(halted 판정은 거래량·종가만 본다). `stock_code`가 None이면 코드를 알 수 없다.
    """

    market: Market
    stock_code: str | None
    reason: str
    trade_volume: int | None = None
    close_price: int | None = None


@dataclass(frozen=True)
class MarketDayRows:
    """한 시장·한 일자 KRX 응답의 파싱 결과. 둘 다 비면 빈 응답(미공개 또는 휴장)."""

    trade_date: date
    market: Market
    quotes: tuple[DailyQuote, ...] = ()
    anomalies: tuple[RowAnomaly, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.quotes and not self.anomalies


# --- DART 수집(S8) ---


@dataclass(frozen=True)
class DartFiling:
    """DART list.json의 증권신고서(지분증권) 한 건."""

    rcept_no: str
    corp_code: str
    corp_name: str
    report_nm: str
    rcept_dt: date
    market: Market | None = None  # corp_cls Y·K·N만 매핑, 그 밖(E 등)은 None
    stock_code: str | None = None


@dataclass(frozen=True)
class StockMasterEntry:
    """corpCode.xml 한 행(상장 종목만)."""

    stock_code: str
    corp_code: str
    corp_name: str


@dataclass(frozen=True)
class IpoListing:
    """상장 예정 종목(ipo_listings 한 행). 상장일은 DART 원문에서 추출한다."""

    corp_code: str
    corp_name: str
    listing_date: date
    source_rcept_no: str
    stock_code: str | None = None
    market: Market | None = None


@dataclass(frozen=True)
class SummaryRecord:
    """summaries 한 행(R14). ok면 summary가 있고, failed면 retry_after가 있다."""

    source_rcept_no: str
    corp_code: str
    status: Literal["ok", "failed"]
    summary: str | None
    retry_after: datetime | None


@dataclass(frozen=True)
class PredictionTarget:
    """한 시점의 예측값(prediction_targets 한 행의 입력). 금액은 원 단위 양의 정수."""

    horizon_months: int
    pred_price: int
    pred_market_cap: int
    price_text: str
    market_cap_text: str


@dataclass(frozen=True)
class ParsedPrediction:
    """검증된 예측 파싱 결과(S10). 날짜 없음. targets는 1·3·6·12개월 순서로 정확히 4개."""

    stock_query: str
    reason: str
    targets: tuple[PredictionTarget, ...]
    warnings: tuple[str, ...] = ()


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
