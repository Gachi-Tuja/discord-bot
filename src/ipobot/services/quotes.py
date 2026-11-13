"""KRX 시세 관측 서비스(R9, market_published).

- 공개(비어 있지 않고 이상 행이 없는) 시장-일자는 DB(krx_market_days·krx_daily_quotes)에 영구 저장하고 재사용한다.
- 빈 응답은 저장하지 않고 메모리 negative 캐시에 5분 둔다. `bypass_negative_cache=True`면 무시한다.
- 이상 행이 있는 시장-일자는 저장하지 않는다. 다음 조회에서 다시 HTTP를 호출한다.
- 인증·일시 오류는 캐시하지 않는다.

`KrxClient`는 이 서비스가 요구하는 포트다. `adapters/krx.py`가 구현하고, 아래 예외를 던진다.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Protocol

from ipobot.clock import Clock
from ipobot.db.repositories import QuoteCacheRepo
from ipobot.domain.models import (
    MARKETS,
    AllEmpty,
    DailyQuote,
    DayObs,
    FetchError,
    Market,
    MarketDayRows,
    RowAnomaly,
    NotTraded,
    Traded,
    Unpublished,
)

log = logging.getLogger(__name__)

NEGATIVE_CACHE_TTL = timedelta(minutes=5)


class KrxAuthError(Exception):
    """KRX 401/403. 설정 오류로 다룬다."""


class KrxFetchError(Exception):
    """그 밖의 HTTP 오류·타임아웃·응답 구조 오류(일시 오류)."""


class KrxClient(Protocol):
    async def fetch_market_day(self, market: Market, trade_date: date) -> MarketDayRows:
        """한 시장·한 일자의 일별 매매정보. 실패 시 KrxAuthError·KrxFetchError."""
        ...


@dataclass(frozen=True)
class _Day:
    """한 시장-일자의 조회 결과(공개)."""

    quotes: dict[str, DailyQuote]
    anomalies: dict[str, RowAnomaly] = field(default_factory=dict)
    unknown_anomaly: bool = False  # 코드를 읽을 수 없는 이상 행이 있다


_Empty = None  # 빈 응답
_Loaded = _Day | None | FetchError


class QuoteService:
    def __init__(self, client: KrxClient, conn: sqlite3.Connection, clock: Clock) -> None:
        self._client = client
        self._repo = QuoteCacheRepo(conn)
        self._clock = clock
        self._negative: dict[tuple[date, Market], datetime] = {}

    async def market_published(
        self, trade_date: date, market: Market, bypass_negative_cache: bool = False
    ) -> bool | FetchError:
        """시장-일자 응답에 행이 1개 이상이면 True, 빈 응답이면 False, 조회 실패면 FetchError."""
        loaded = await self._load(trade_date, market, bypass_negative_cache)
        if isinstance(loaded, FetchError):
            return loaded
        return loaded is not None

    async def find_by_name(
        self, corp_name: str, trade_date: date, bypass_negative_cache: bool = False
    ) -> list[DailyQuote] | None | FetchError:
        """R7 3단계: 그 날 3시장 스냅샷에서 `ISU_NM`이 정확히 같은 행.

        3시장이 모두 공개됐을 때만 목록(0개 가능)을 돌려준다. 하나라도 빈 응답이면 None(스냅샷 불완전),
        조회 실패면 FetchError(인증 우선)다.
        """
        results = await asyncio.gather(*(self._load(trade_date, m, bypass_negative_cache) for m in MARKETS))
        errors = [r for r in results if isinstance(r, FetchError)]
        if errors:
            return FetchError("auth") if any(e.kind == "auth" for e in errors) else FetchError("transient")
        if any(r is None for r in results):
            return None
        name = corp_name.strip()
        return [q for day in results for q in day.quotes.values() if q.corp_name.strip() == name]

    async def day_obs(
        self,
        code: str,
        trade_date: date,
        hint: Market | None,
        bypass_negative_cache: bool = False,
    ) -> DayObs:
        """R9 분류표. 힌트 시장을 먼저 조회하고, 코드를 찾지 못했을 때만 나머지 시장을 조회한다."""
        order: list[Market] = [hint] if hint is not None else []
        order += [m for m in MARKETS if m != hint]
        loaded: dict[Market, _Loaded] = {}

        if hint is not None:
            loaded[hint] = await self._load(trade_date, hint, bypass_negative_cache)
            if isinstance(loaded[hint], FetchError) or self._classify_in(code, loaded[hint]) is not None:
                return self._classify(code, trade_date, hint, loaded)
        rest = [m for m in order if m not in loaded]
        results = await asyncio.gather(*(self._load(trade_date, m, bypass_negative_cache) for m in rest))
        loaded.update(zip(rest, results))
        return self._classify(code, trade_date, hint, loaded)

    # --- 분류 ---

    @staticmethod
    def _classify_in(code: str, day: _Loaded) -> DayObs | None:
        """한 시장에서 코드 행을 찾았을 때의 결과. 못 찾았거나 비공개·오류면 None."""
        if not isinstance(day, _Day):
            return None
        quote = day.quotes.get(code)
        if quote is not None:
            if quote.trade_volume == 0 or quote.close_price == 0:
                return NotTraded("halted")
            if quote.market_cap <= 0:
                return FetchError("data_anomaly")
            return Traded(quote)
        if code in day.anomalies:
            # 필수 필드가 깨진 행은 거래량·종가가 0이어도 halted로 보지 않는다(Part B §8 1단계).
            return FetchError("data_anomaly")
        return None

    def _classify(
        self, code: str, trade_date: date, hint: Market | None, loaded: dict[Market, _Loaded]
    ) -> DayObs:
        values = list(loaded.values())
        if any(isinstance(v, FetchError) and v.kind == "auth" for v in values):
            return FetchError("auth")
        if any(isinstance(v, FetchError) for v in values):
            return FetchError("transient")
        for market in loaded:
            found = self._classify_in(code, loaded[market])
            if found is None:
                continue
            if isinstance(found, FetchError):
                day = loaded[market]
                anomaly = day.anomalies.get(code) if isinstance(day, _Day) else None
                log.warning(
                    "KRX data_anomaly code=%s date=%s market=%s reason=%s volume=%s close=%s",
                    code,
                    trade_date,
                    market,
                    anomaly.reason if anomaly else "market_cap<=0",
                    anomaly.trade_volume if anomaly else None,
                    anomaly.close_price if anomaly else None,
                )
            if isinstance(found, Traded) and hint is not None and market != hint:
                log.info("market_moved code=%s date=%s hint=%s actual=%s", code, trade_date, hint, market)
            return found
        published = {m for m, v in loaded.items() if isinstance(v, _Day)}
        if any(isinstance(v, _Day) and v.unknown_anomaly for v in loaded.values()):
            # 코드를 읽을 수 없는 이상 행이 그 코드일 수 있으므로 부재로 단정하지 않는다.
            log.warning("KRX data_anomaly(unknown code row) code=%s date=%s", code, trade_date)
            return FetchError("data_anomaly")
        if len(published) == len(MARKETS):
            return NotTraded("absent")
        if not published:
            return AllEmpty()
        return Unpublished(frozenset(m for m in MARKETS if m not in published))

    # --- 조회 + 캐시 ---

    async def _load(self, trade_date: date, market: Market, bypass_negative_cache: bool) -> _Loaded:
        cached = self._repo.load_day(trade_date, market)
        if cached is not None:
            return _Day(quotes={q.stock_code: q for q in cached})
        key = (trade_date, market)
        now = self._clock.now()
        if not bypass_negative_cache:
            expires = self._negative.get(key)
            if expires is not None:
                if now < expires:
                    return _Empty
                del self._negative[key]
        try:
            rows = await self._client.fetch_market_day(market, trade_date)
        except KrxAuthError:
            log.error("KRX 인증 실패 market=%s date=%s", market, trade_date)
            return FetchError("auth")
        except KrxFetchError as exc:
            log.warning("KRX 조회 실패 market=%s date=%s: %s", market, trade_date, exc)
            return FetchError("transient")
        if rows.is_empty:
            self._negative[key] = now + NEGATIVE_CACHE_TTL
            return _Empty
        self._negative.pop(key, None)
        day = _build_day(rows.quotes, rows.anomalies)
        if rows.anomalies:
            log.warning(
                "KRX 이상 행 %d개 market=%s date=%s: 시장-일자를 저장하지 않습니다",
                len(rows.anomalies),
                market,
                trade_date,
            )
        elif any(q.market_cap <= 0 and q.trade_volume > 0 and q.close_price > 0 for q in rows.quotes):
            log.warning("KRX 시총 0 이하 행 market=%s date=%s: 시장-일자를 저장하지 않습니다", market, trade_date)
        else:
            self._repo.save_day(trade_date, market, rows.quotes, now)
        return day


def _build_day(quotes: Iterable[DailyQuote], anomalies: Iterable[RowAnomaly]) -> _Day:
    anomaly_by_code: dict[str, RowAnomaly] = {}
    unknown = False
    for a in anomalies:
        code = a.stock_code
        if code is None:
            unknown = True
        else:
            anomaly_by_code[code] = a
    return _Day(quotes={q.stock_code: q for q in quotes}, anomalies=anomaly_by_code, unknown_anomaly=unknown)
