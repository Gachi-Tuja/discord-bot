"""S6: QuoteService 분류표(R9)·캐시(fake KrxClient + 실제 sqlite)."""

from __future__ import annotations

import logging
from datetime import date, timedelta

import pytest

from ipobot.db.connection import open_database
from ipobot.domain.models import (
    AllEmpty,
    DailyQuote,
    FetchError,
    MarketDayRows,
    NotTraded,
    RowAnomaly,
    Traded,
    Unpublished,
)
from ipobot.services.quotes import KrxAuthError, KrxFetchError, QuoteService

D = date(2026, 11, 10)


def q(code, market="KOSPI", vol=1000, close=10_000, mcap=10**11, d=D):
    return DailyQuote(d, market, code, f"종목{code}", close, close, vol, mcap, 10**7)


class FakeKrx:
    """시장별 응답을 돌려준다. 값: MarketDayRows용 (quotes, anomalies) 튜플·예외·None(빈 응답)."""

    def __init__(self, responses=None):
        self.responses = dict(responses or {})
        self.calls: list[tuple[str, date]] = []

    async def fetch_market_day(self, market, trade_date):
        self.calls.append((market, trade_date))
        r = self.responses.get(market)
        if isinstance(r, Exception):
            raise r
        if r is None:
            return MarketDayRows(trade_date, market)
        quotes, anomalies = r
        return MarketDayRows(trade_date, market, tuple(quotes), tuple(anomalies))


@pytest.fixture
def conn(tmp_path):
    c = open_database(tmp_path / "q.sqlite3")
    yield c
    c.close()


def svc(conn, fake_clock, responses):
    fake = FakeKrx(responses)
    return QuoteService(fake, conn, fake_clock), fake


def market_days(conn):
    return {(r[0], r[1]) for r in conn.execute("SELECT trade_date, market FROM krx_market_days")}


async def test_hint_market_has_code_single_call(conn, fake_clock):
    s, fake = svc(conn, fake_clock, {"KOSPI": ([q("005930")], []), "KOSDAQ": ([q("111111", "KOSDAQ")], [])})
    obs = await s.day_obs("005930", D, "KOSPI")
    assert isinstance(obs, Traded) and obs.quote.close_price == 10_000
    assert fake.calls == [("KOSPI", D)]


async def test_market_moved_fallback(conn, fake_clock, caplog):
    s, fake = svc(
        conn,
        fake_clock,
        {"KOSPI": ([q("005930")], []), "KOSDAQ": ([q("0158S0", "KOSDAQ")], []), "KONEX": ([q("999999", "KONEX")], [])},
    )
    with caplog.at_level(logging.INFO):
        obs = await s.day_obs("0158S0", D, "KOSPI")
    assert isinstance(obs, Traded) and obs.quote.market == "KOSDAQ"
    assert "market_moved" in caplog.text


async def test_konex_unpublished_code_missing(conn, fake_clock):
    s, _ = svc(conn, fake_clock, {"KOSPI": ([q("005930")], []), "KOSDAQ": ([q("111111", "KOSDAQ")], [])})
    assert await s.day_obs("222222", D, "KONEX") == Unpublished(frozenset({"KONEX"}))


async def test_all_published_absent(conn, fake_clock):
    s, _ = svc(
        conn,
        fake_clock,
        {"KOSPI": ([q("005930")], []), "KOSDAQ": ([q("111111", "KOSDAQ")], []), "KONEX": ([q("333333", "KONEX")], [])},
    )
    assert await s.day_obs("222222", D, "KOSPI") == NotTraded("absent")


@pytest.mark.parametrize(("market", "vol", "close"), [("KOSPI", 0, 10_000), ("KONEX", 0, 0), ("KOSDAQ", 5, 0)])
async def test_halted(conn, fake_clock, market, vol, close):
    s, _ = svc(conn, fake_clock, {market: ([q("123456", market, vol=vol, close=close, mcap=0)], [])})
    assert await s.day_obs("123456", D, market) == NotTraded("halted")


async def test_all_empty(conn, fake_clock):
    s, fake = svc(conn, fake_clock, {})
    assert await s.day_obs("005930", D, None) == AllEmpty()
    assert len(fake.calls) == 3
    assert market_days(conn) == set()


async def test_partial_publication_is_unpublished_not_absent(conn, fake_clock):
    s, _ = svc(conn, fake_clock, {"KOSPI": ([q("005930")], [])})
    assert await s.day_obs("247540", D, "KOSDAQ") == Unpublished(frozenset({"KOSDAQ", "KONEX"}))


async def test_hint_none_queries_all_markets(conn, fake_clock):
    s, fake = svc(
        conn,
        fake_clock,
        {"KOSPI": ([q("005930")], []), "KOSDAQ": ([q("247540", "KOSDAQ")], []), "KONEX": ([q("333333", "KONEX")], [])},
    )
    obs = await s.day_obs("247540", D, None)
    assert isinstance(obs, Traded) and obs.quote.market == "KOSDAQ"
    assert {m for m, _ in fake.calls} == {"KOSPI", "KOSDAQ", "KONEX"}


async def test_mktcap_dash_is_data_anomaly_not_persisted(conn, fake_clock, caplog):
    responses = {"KOSDAQ": ([q("111110", "KOSDAQ", mcap=0), q("247540", "KOSDAQ")], [])}
    s, fake = svc(conn, fake_clock, responses)
    with caplog.at_level(logging.WARNING):
        assert await s.day_obs("111110", D, "KOSDAQ") == FetchError("data_anomaly")
    assert "data_anomaly" in caplog.text
    assert market_days(conn) == set()
    await s.day_obs("111110", D, "KOSDAQ")
    assert fake.calls.count(("KOSDAQ", D)) == 2


async def test_missing_mktcap_key_anomaly_other_row_traded(conn, fake_clock):
    anomaly = RowAnomaly("KOSDAQ", "222220", "MKTCAP 결측", trade_volume=100, close_price=5_000)
    s, fake = svc(conn, fake_clock, {"KOSDAQ": ([q("247540", "KOSDAQ")], [anomaly])})
    assert await s.day_obs("222220", D, "KOSDAQ") == FetchError("data_anomaly")
    obs = await s.day_obs("247540", D, "KOSDAQ")
    assert isinstance(obs, Traded)
    assert market_days(conn) == set()
    assert fake.calls.count(("KOSDAQ", D)) == 2


async def test_unknown_code_anomaly_blocks_absent(conn, fake_clock):
    anomaly = RowAnomaly("KOSPI", None, "ISU_CD 결측")
    s, _ = svc(
        conn,
        fake_clock,
        {"KOSPI": ([q("005930")], [anomaly]), "KOSDAQ": ([q("111111", "KOSDAQ")], []), "KONEX": ([q("222222", "KONEX")], [])},
    )
    assert await s.day_obs("777777", D, "KOSPI") == FetchError("data_anomaly")
    assert isinstance(await s.day_obs("005930", D, "KOSPI"), Traded)


@pytest.mark.parametrize(("exc", "kind"), [(KrxAuthError("403"), "auth"), (KrxFetchError("503"), "transient")])
async def test_fetch_errors_not_cached(conn, fake_clock, exc, kind):
    s, fake = svc(conn, fake_clock, {"KOSPI": exc})
    assert await s.day_obs("005930", D, "KOSPI") == FetchError(kind)
    assert await s.market_published(D, "KOSPI") == FetchError(kind)
    assert fake.calls.count(("KOSPI", D)) == 2


async def test_auth_wins_over_transient(conn, fake_clock):
    s, _ = svc(conn, fake_clock, {"KOSDAQ": KrxFetchError("timeout"), "KONEX": KrxAuthError("401")})
    assert await s.day_obs("005930", D, None) == FetchError("auth")


async def test_published_cache_reused(conn, fake_clock):
    s, fake = svc(conn, fake_clock, {"KOSPI": ([q("005930")], [])})
    first = await s.day_obs("005930", D, "KOSPI")
    assert market_days(conn) == {(D.isoformat(), "KOSPI")}
    fresh = QuoteService(FakeKrx(), conn, fake_clock)  # 재시작 후에도 DB에서 읽는다
    assert await fresh.day_obs("005930", D, "KOSPI") == first
    assert await s.market_published(D, "KOSPI") is True
    assert fake.calls == [("KOSPI", D)]


async def test_negative_cache_ttl_and_bypass(conn, fake_clock):
    s, fake = svc(conn, fake_clock, {})
    assert await s.market_published(D, "KOSPI") is False
    assert await s.market_published(D, "KOSPI") is False
    assert len(fake.calls) == 1
    fake_clock.set(fake_clock.now() + timedelta(minutes=4, seconds=59))
    assert await s.market_published(D, "KOSPI") is False
    assert len(fake.calls) == 1
    fake_clock.set(fake_clock.now() + timedelta(seconds=1))
    assert await s.market_published(D, "KOSPI") is False
    assert len(fake.calls) == 2
    # 우회: 캐시가 「미공개」여도 실제 공개면 공개로 본다
    fake.responses["KOSPI"] = ([q("005930")], [])
    assert await s.market_published(D, "KOSPI") is False  # 캐시 적중
    assert await s.market_published(D, "KOSPI", bypass_negative_cache=True) is True
    assert market_days(conn) == {(D.isoformat(), "KOSPI")}


async def test_day_obs_bypass_negative_cache(conn, fake_clock):
    s, fake = svc(conn, fake_clock, {})
    assert await s.day_obs("005930", D, None) == AllEmpty()
    fake.responses.update(
        {"KOSPI": ([q("005930")], []), "KOSDAQ": ([q("111111", "KOSDAQ")], []), "KONEX": ([q("222222", "KONEX")], [])}
    )
    assert await s.day_obs("005930", D, None) == AllEmpty()
    assert len(fake.calls) == 3
    assert isinstance(await s.day_obs("005930", D, None, bypass_negative_cache=True), Traded)
