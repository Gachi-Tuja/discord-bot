"""S11: 예측 초안·확정(R1·R5~R8·R12). 실제 QuoteService + 날짜별 fake KRX + 실제 sqlite."""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

import pytest

from ipobot.bot.formatting import (
    REJECT_BASE_DATE_CHANGED,
    REJECT_DUPLICATE,
    REJECT_GROUP_CLOSED,
    REJECT_KRX_FAILED,
    REJECT_KRX_UNPUBLISHED,
    REJECT_NO_BASE_PRICE,
    REJECT_STOCK_AMBIGUOUS,
    REJECT_STOCK_CHANGED,
    REJECT_STOCK_NOT_FOUND,
)
from ipobot.clock import KST, utc_iso
from ipobot.db.connection import open_database
from ipobot.db.repositories import StockMasterRepo
from ipobot.domain.calendar import KrxCalendar
from ipobot.domain.models import DailyQuote, MarketDayRows, RowAnomaly, StockMasterEntry
from ipobot.services import alerts
from ipobot.services.prediction_parser import PredictionParser
from ipobot.services.predictions import Confirmed, Draft, PredictionService, Rejected
from ipobot.services.quotes import KrxAuthError, KrxFetchError, QuoteService
from tests.conftest import FakeClock

THU = date(2026, 11, 12)
FRI = date(2026, 11, 13)
MON = date(2026, 11, 16)
CODE = "0158S0"
MARKETS = ("KOSPI", "KOSDAQ", "KONEX")


def at(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=KST)


def q(code=CODE, d=THU, market="KOSDAQ", name="앨리스", close=20_000, vol=1000, mcap=200_000_000_000, shares=10**7):
    return DailyQuote(d, market, code, name, close, close, vol, mcap, shares)


class DatedKrx:
    """(시장, 일자)별 응답. 값: DailyQuote 목록·(목록, 이상행) 튜플·예외. 없으면 빈 응답."""

    def __init__(self) -> None:
        self.days: dict[tuple[str, date], object] = {}
        self.calls: list[tuple[str, date]] = []

    def publish(self, d: date, *quotes: DailyQuote, anomalies=()) -> None:
        for m in MARKETS:
            rows = [x for x in quotes if x.market == m]
            filler = [q(code="999990", d=d, market=m, name=f"채움{m}")]
            self.days[(m, d)] = (rows + filler, [a for a in anomalies if a.market == m])

    async def fetch_market_day(self, market, trade_date):
        self.calls.append((market, trade_date))
        r = self.days.get((market, trade_date))
        if isinstance(r, Exception):
            raise r
        if r is None:
            return MarketDayRows(trade_date, market)
        quotes, anomalies = r
        return MarketDayRows(trade_date, market, tuple(quotes), tuple(anomalies))


def payload(stock: str) -> dict:
    return {
        "stock": stock,
        "reason": "보호예수 해제 전까지 강세",
        "targets": [
            {"horizon_months": h, "price_text": f"{p:,}원", "price": p, "market_cap_text": cap_text, "market_cap": cap}
            for h, p, cap_text, cap in (
                (1, 31_500, "3,150억", 315_000_000_000),
                (3, 30_000, "3,000억", 300_000_000_000),
                (6, 25_000, "2,500억", 250_000_000_000),
                (12, 22_000, "2,200억", 220_000_000_000),
            )
        ],
    }


class FakeLlm:
    def __init__(self) -> None:
        self.next: dict | None = None

    async def parse_prediction(self, text):
        return self.next if self.next is not None else payload(text)


@pytest.fixture
def conn(tmp_path):
    c = open_database(tmp_path / "p.sqlite3")
    yield c
    c.close()


@pytest.fixture
def env(conn):
    clock = FakeClock(at(MON, 10))
    alerts.set_default(alerts.OperatorAlerts(clock))
    krx = DatedKrx()
    llm = FakeLlm()
    service = PredictionService(
        PredictionParser(llm),
        QuoteService(krx, conn, clock),
        conn,
        clock,
        KrxCalendar(),
        lookback=3,
        judge_time=time(9, 0),
    )
    yield service, krx, clock, llm
    alerts.set_default(alerts.OperatorAlerts(alerts.SystemClock()))


def counts(conn) -> tuple[int, int, int]:
    return tuple(  # type: ignore[return-value]
        conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("predictions", "prediction_targets", "judgments")
    )


# --- R1 기준일 ---


async def test_monday_morning_friday_unpublished_uses_thursday(env):
    service, krx, clock, _ = env
    krx.publish(THU, q(d=THU, close=20_000))
    clock.set(at(MON, 8))
    d = await service.draft("u1", "앨리스팬", CODE)
    assert isinstance(d, Draft) and d.base_date == THU and d.base.close_price == 20_000

    krx.publish(FRI, q(d=FRI, close=21_000))
    clock.set(at(MON, 10))  # negative 캐시(5분) 만료 뒤
    d = await service.draft("u1", "앨리스팬", CODE)
    assert isinstance(d, Draft) and d.base_date == FRI and d.base.close_price == 21_000
    assert d.base.market == "KOSDAQ"


async def test_open_day_with_empty_krx_falls_back_to_earlier_published(env):
    service, krx, clock, _ = env
    krx.publish(THU, q(d=THU))  # 금요일은 캘린더상 개장이지만 KRX 빈 응답(휴장 추정)
    clock.set(at(MON, 15))
    d = await service.draft("u1", "n", CODE)
    assert isinstance(d, Draft) and d.base_date == THU


async def test_all_lookback_days_empty_is_unpublished_rejection(env):
    service, _, _, _ = env
    assert await service.draft("u1", "n", CODE) == Rejected(REJECT_KRX_UNPUBLISHED)


async def test_halted_row_rejects_with_no_base_price(env):
    service, krx, _, _ = env
    krx.publish(FRI, q(d=FRI, vol=0))
    assert await service.draft("u1", "n", CODE) == Rejected(REJECT_NO_BASE_PRICE)


async def test_absent_code_rejects_with_no_base_price(env):
    service, krx, _, _ = env
    krx.publish(FRI, q(d=FRI))
    assert await service.draft("u1", "n", "123450") == Rejected(REJECT_NO_BASE_PRICE)


@pytest.mark.parametrize("kind", ["transient", "auth", "data_anomaly"])
async def test_fetch_errors_reject_and_auth_alerts(env, kind, caplog):
    service, krx, _, _ = env
    if kind == "data_anomaly":
        krx.publish(FRI, anomalies=[RowAnomaly("KOSDAQ", CODE, "TDD_CLSPRC 결측")])
    else:
        exc = KrxAuthError("401") if kind == "auth" else KrxFetchError("timeout")
        for m in MARKETS:
            krx.days[(m, FRI)] = exc
    with caplog.at_level(logging.ERROR):
        assert await service.draft("u1", "n", CODE) == Rejected(REJECT_KRX_FAILED)
    assert ("OPERATOR_ALERT krx_auth" in caplog.text) == (kind == "auth")


async def test_ipo_listing_market_is_used_as_hint(env, conn):
    service, krx, clock, _ = env
    conn.execute(
        "INSERT INTO ipo_listings (corp_code, corp_name, stock_code, market, listing_date, source_rcept_no, collected_at)"
        " VALUES ('00000001', '앨리스', ?, 'KOSDAQ', '2026-11-02', 'r1', ?)",
        (CODE, utc_iso(clock.now())),
    )
    krx.publish(FRI, q(d=FRI))
    d = await service.draft("u1", "n", CODE)
    assert isinstance(d, Draft)
    assert krx.calls == [("KOSDAQ", FRI)]


# --- R7 종목 해석 ---


async def test_name_resolves_via_stock_master(env, conn):
    service, krx, clock, _ = env
    StockMasterRepo(conn).upsert_all([StockMasterEntry(stock_code=CODE, corp_code="00000001", corp_name="앨리스")], clock.now())
    krx.publish(FRI, q(d=FRI))
    d = await service.draft("u1", "n", "앨리스")
    assert isinstance(d, Draft) and d.stock_code == CODE


async def test_new_listing_missing_from_corpcode_resolves_via_krx_isu_nm(env):
    service, krx, _, _ = env
    krx.publish(FRI, q(d=FRI, name="신규상장"))
    d = await service.draft("u1", "n", "신규상장")
    assert isinstance(d, Draft) and d.stock_code == CODE and d.base.corp_name == "신규상장"


async def test_name_lookup_uses_latest_published_snapshot(env, conn):
    service, krx, clock, _ = env
    krx.publish(THU, q(d=THU, name="신규상장"))
    clock.set(at(MON, 8))  # 금요일 미공개 → 목요일 스냅샷
    d = await service.draft("u1", "n", "신규상장")
    assert isinstance(d, Draft) and d.base_date == THU


async def test_same_name_in_krx_snapshot_is_rejected(env):
    service, krx, _, _ = env
    krx.publish(FRI, q(d=FRI, name="동명"), q(code="111110", d=FRI, market="KOSPI", name="동명"))
    assert await service.draft("u1", "n", "동명") == Rejected(REJECT_STOCK_AMBIGUOUS)


async def test_same_name_in_stock_master_is_rejected(env, conn):
    service, _, clock, _ = env
    StockMasterRepo(conn).upsert_all(
        [StockMasterEntry(stock_code=CODE, corp_code="00000001", corp_name="동명"), StockMasterEntry(stock_code="111110", corp_code="00000002", corp_name="동명")], clock.now()
    )
    assert await service.draft("u1", "n", "동명") == Rejected(REJECT_STOCK_AMBIGUOUS)


async def test_unknown_name_is_rejected(env):
    service, krx, _, _ = env
    krx.publish(FRI, q(d=FRI))
    assert await service.draft("u1", "n", "없는회사") == Rejected(REJECT_STOCK_NOT_FOUND)


async def test_parser_rejections_pass_through(env):
    service, krx, _, llm = env
    krx.publish(FRI, q(d=FRI))
    p = payload(CODE)
    p["targets"] = p["targets"][:3]
    llm.next = p
    r = await service.draft("u1", "n", CODE)
    assert isinstance(r, Rejected) and r.message.startswith("누락된 시점")
    p = payload(CODE)
    p["targets"][0]["price"] = 99_999
    llm.next = p
    r = await service.draft("u1", "n", CODE)
    assert isinstance(r, Rejected) and r.message.startswith("금액 해석 불일치")
    assert krx.calls == []  # 파싱 거부는 KRX를 부르지 않는다


def test_rejection_messages_are_unique():
    messages = [
        "누락된 시점",
        REJECT_DUPLICATE,
        REJECT_STOCK_NOT_FOUND,
        REJECT_STOCK_AMBIGUOUS,
        REJECT_KRX_UNPUBLISHED,
        REJECT_KRX_FAILED,
        REJECT_NO_BASE_PRICE,
        REJECT_BASE_DATE_CHANGED,
        REJECT_STOCK_CHANGED,
        "금액 해석 불일치",
        REJECT_GROUP_CLOSED,
    ]
    assert len(set(messages)) == len(messages)


async def test_implied_shares_warning_does_not_block(env):
    service, krx, _, llm = env
    krx.publish(FRI, q(d=FRI, shares=10**5))  # 31,500원 × 10만주 = 31.5억 ≪ 3,150억
    d = await service.draft("u1", "n", CODE)
    assert isinstance(d, Draft)
    assert sum("10배 이상" in w for w in d.warnings) == 4


# --- confirm_draft (R5·R6·R8·R12) ---


async def test_confirm_success_writes_one_prediction_four_targets_four_judgments(env, conn):
    service, krx, clock, _ = env
    krx.publish(FRI, q(d=FRI, close=21_000, mcap=210_000_000_000))
    d = await service.draft("u1", "앨리스팬", CODE)
    c = await service.confirm_draft(d)
    assert isinstance(c, Confirmed)
    assert counts(conn) == (1, 4, 4)
    p = conn.execute("SELECT * FROM predictions").fetchone()
    assert (p["stock_code"], p["market"], p["base_date"], p["base_price"], p["base_market_cap"]) == (
        CODE, "KOSDAQ", "2026-11-13", 21_000, 210_000_000_000,
    )
    targets = {r["horizon_months"]: r["target_date"] for r in conn.execute("SELECT * FROM prediction_targets")}
    assert targets == {1: "2026-12-13", 3: "2027-02-13", 6: "2027-05-13", 12: "2027-11-13"}
    rows = {r["horizon_months"]: r for r in conn.execute("SELECT * FROM judgments")}
    # 1M: 12/13(일) → 12/14(월) 판정, 다음 개장일 12/15 09:00 KST 첫 시도
    j = rows[1]
    assert j["judge_trade_date"] == "2026-12-14"
    assert j["next_attempt_at"] == utc_iso(at(date(2026, 12, 15), 9))
    assert (j["status"], j["unavailable_since"], j["unavailable_last_day"], j["unavailable_days"]) == (
        "pending", None, None, 0,
    )
    assert (j["announce_parts_sent"], j["not_traded_days"], j["market"]) == (0, 0, "KOSDAQ")
    # 12M: 2027-11-13(토) → 11/15(월), 첫 시도 11/16 09:00
    assert rows[12]["judge_trade_date"] == "2027-11-15"
    assert rows[12]["next_attempt_at"] == utc_iso(at(date(2027, 11, 16), 9))


async def test_confirm_recheck_bypasses_negative_cache_and_rejects_base_change(env, conn):
    service, krx, clock, _ = env
    krx.publish(THU, q(d=THU))
    clock.set(at(MON, 8, 0))
    d = await service.draft("u1", "n", CODE)
    assert isinstance(d, Draft) and d.base_date == THU
    krx.publish(FRI, q(d=FRI))
    clock.set(at(MON, 8, 1))  # negative 캐시 TTL(5분) 안
    assert await service.confirm_draft(d) == Rejected(REJECT_BASE_DATE_CHANGED)
    assert counts(conn) == (0, 0, 0)


async def test_confirm_rejects_when_resolution_changes(env, conn):
    service, krx, clock, _ = env
    krx.publish(FRI, q(d=FRI, name="개명"))
    d = await service.draft("u1", "n", "개명")
    assert isinstance(d, Draft)
    StockMasterRepo(conn).upsert_all([StockMasterEntry(stock_code="222220", corp_code="00000009", corp_name="개명")], clock.now())
    assert await service.confirm_draft(d) == Rejected(REJECT_STOCK_CHANGED)
    assert counts(conn) == (0, 0, 0)


async def test_second_user_reuses_group_judgments(env, conn):
    service, krx, _, _ = env
    krx.publish(FRI, q(d=FRI))
    for user in ("u1", "u2"):
        assert isinstance(await service.confirm_draft(await service.draft(user, user, CODE)), Confirmed)
    assert counts(conn) == (2, 8, 4)


async def test_same_user_resubmission_is_rejected_without_partial_rows(env, conn):
    service, krx, _, _ = env
    krx.publish(FRI, q(d=FRI))
    first = await service.draft("u1", "n", CODE)
    second = await service.draft("u1", "n", CODE)  # 첫 확정 전 초안 두 개
    assert isinstance(await service.confirm_draft(first), Confirmed)
    assert await service.confirm_draft(second) == Rejected(REJECT_DUPLICATE)
    assert counts(conn) == (1, 4, 4)
    assert await service.draft("u1", "n", CODE) == Rejected(REJECT_DUPLICATE)  # 사전 검사


async def test_closed_group_rolls_back_whole_confirm(env, conn):
    service, krx, _, _ = env
    krx.publish(FRI, q(d=FRI))
    assert isinstance(await service.confirm_draft(await service.draft("u1", "n", CODE)), Confirmed)
    late = await service.draft("u2", "n", CODE)
    assert isinstance(late, Draft)
    conn.execute(
        "UPDATE judgments SET status = 'done', actual_trade_date = judge_trade_date, actual_price = 1,"
        " actual_market_cap = 1 WHERE horizon_months = 1"
    )
    assert await service.confirm_draft(late) == Rejected(REJECT_GROUP_CLOSED)
    assert counts(conn) == (1, 4, 4)
    assert await service.draft("u3", "n", CODE) == Rejected(REJECT_GROUP_CLOSED)  # 사전 검사


async def test_confirm_uses_rechecked_snapshot_after_cache_expiry(env, conn):
    """확정 재확인은 같은 기준일이면 저장된 시세(DB 캐시)를 그대로 쓴다."""
    service, krx, clock, _ = env
    krx.publish(FRI, q(d=FRI, close=21_000))
    d = await service.draft("u1", "n", CODE)
    clock.set(clock.now() + timedelta(minutes=9))
    krx.days.clear()  # 공개된 시장-일자는 DB에 저장돼 HTTP 없이 재사용된다
    assert isinstance(await service.confirm_draft(d), Confirmed)
    assert conn.execute("SELECT base_price FROM predictions").fetchone()[0] == 21_000
