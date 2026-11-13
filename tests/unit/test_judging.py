"""S12 판정 서비스: 실제 sqlite + 실제 QuoteService(날짜별 fake KRX) + fake 디스코드 채널."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from ipobot.bot.formatting import text_parts
from ipobot.clock import KST, utc_iso
from ipobot.db.connection import open_database
from ipobot.db.repositories import CalendarRepo, JudgmentRepo, NewJudgment, NewPrediction, PredictionRepo
from ipobot.domain import judging_policy
from ipobot.domain.calendar import KrxCalendar
from ipobot.domain.models import DailyQuote, MarketDayRows, PredictionTarget, RowAnomaly
from ipobot.services import judging as judging_mod
from ipobot.services.alerts import OperatorAlerts
from ipobot.services.delivery import part_nonce
from ipobot.services.judging import JudgingService
from ipobot.services.quotes import KrxAuthError, KrxFetchError, QuoteService
from tests.conftest import FakeClock
from tests.fakes.discord_channel import FakeDiscord, forbidden

CODE = "0158S0"
OTHER = "99999"  # + 시장 끝 글자 = 6자리 코드
BASE = date(2026, 11, 10)
TARGET = date(2026, 12, 10)  # 목요일
D1, D2, D3 = date(2026, 12, 10), date(2026, 12, 11), date(2026, 12, 14)


def at(d: date, hh: int = 9, mm: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=KST)


def q(d, code=CODE, market="KOSDAQ", close=36_000, mcap=720_000_000_000, vol=1000):
    return DailyQuote(d, market, code, f"종목{code}", close, close, vol, mcap, 20_000_000)


class DatedKrx:
    """(시장, 일자)별 응답. 값: quotes 리스트 | (quotes, anomalies) | 예외 | 리스트의 리스트(호출마다 소비).

    등록되지 않은 키는 빈 응답(미공개·휴장). `default(d)`가 있으면 그 날 응답을 만든다.
    """

    def __init__(self, default=None):
        self.responses: dict[tuple[str, date], object] = {}
        self.default = default
        self.calls: list[tuple[str, date]] = []

    def set(self, market, d, value):
        self.responses[(market, d)] = value

    def script(self, market, d, *values):
        self.responses[(market, d)] = _Script(list(values))

    async def fetch_market_day(self, market, trade_date):
        self.calls.append((market, trade_date))
        key = (market, trade_date)
        r = self.responses.get(key)
        if r is None and self.default is not None:
            r = self.default(market, trade_date)
        if isinstance(r, _Script):
            r = r.values.pop(0) if len(r.values) > 1 else r.values[0]
        if isinstance(r, Exception):
            raise r
        if r is None:
            return MarketDayRows(trade_date, market)
        quotes, anomalies = r if isinstance(r, tuple) else (r, [])
        return MarketDayRows(trade_date, market, tuple(quotes), tuple(anomalies))


class _Script:
    def __init__(self, values):
        self.values = values


def published_without_code(market, d):
    """세 시장 모두 공개됐지만 대상 종목은 없다."""
    return [q(d, code=OTHER + market[-1], market=market)]


class LaborOpenCalendar(KrxCalendar):
    """근로자의 날을 개장일로 보는 캘린더(달력은 열렸지만 KRX는 빈 날 시나리오용)."""

    def rule_is_open(self, d):
        if d.month == 5 and d.day == 1:
            return d.weekday() < 5
        return super().rule_is_open(d)


class Env:
    def __init__(self, tmp_path, *, now=None, krx=None, cal_cls=KrxCalendar, discord_script=None):
        self.conn = open_database(tmp_path / "j.sqlite3")
        self.clock = FakeClock(now or at(D2))
        self.krx = krx or DatedKrx()
        self.quotes = QuoteService(self.krx, self.conn, self.clock)
        self.cal = cal_cls(source=CalendarRepo(self.conn))
        self.fake = FakeDiscord(script=discord_script, clock=self.clock)
        self.alerts: list[tuple[str, str]] = []
        self.operator = OperatorAlerts(self.clock)

        def alert(kind, detail):
            self.alerts.append((kind, detail))
            return self.operator.alert(kind, detail)

        self.svc = JudgingService(
            conn=self.conn,
            quotes=self.quotes,
            cal=self.cal,
            channel=self.fake.channel(),
            clock=self.clock,
            alert=alert,
            delivery_options={"sleep": _no_sleep, "retry_delays": (), "alert": lambda kind, detail: True},
        )
        self.judgments = JudgmentRepo(self.conn)

    def add_group(self, *, code=CODE, market="KOSDAQ", base=BASE, target=TARGET, users=("u1", "u2"), judge_day=None):
        repo = PredictionRepo(self.conn)
        judge_day = judge_day or target
        nj = NewJudgment(code, market, base, 1, target, judge_day, at(judge_day))
        for i, user in enumerate(users):
            repo.confirm(
                NewPrediction(
                    discord_user_id=user,
                    discord_user_name=f"이름{user}",
                    stock_code=code,
                    corp_name=f"회사{code}",
                    market=market,
                    base_date=base,
                    base_price=30_000,
                    base_market_cap=600_000_000_000,
                    reason="성장",
                    raw_text="...",
                    targets=((PredictionTarget(1, 33_000 + i * 2_000, 650_000_000_000, "3.3만", "6500억"), target),),
                ),
                [nj],
                at(BASE),
            )
        return self.conn.execute(
            "SELECT id FROM judgments WHERE stock_code = ? AND base_date = ?", (code, base.isoformat())
        ).fetchone()["id"]

    def row(self, jid):
        return self.judgments.get(jid)

    async def tick(self, now=None):
        if now is not None:
            self.clock.set(now)
        await self.svc.run_judging()
        await self.svc.run_announcements()


async def _no_sleep(_):
    return None


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.conn.close()


def closed_days(conn):
    return {
        (r["trade_date"], r["evidence_date"])
        for r in conn.execute("SELECT trade_date, evidence_date FROM krx_closed_days")
    }


# ---------------------------------------------------------------- 정시 판정·공지


async def test_on_time_done_and_announced_same_tick(env):
    jid = env.add_group()
    env.krx.set("KOSDAQ", D1, [q(D1)])
    await env.tick(at(D2))
    row = env.row(jid)
    assert row["status"] == "done"
    assert (row["actual_trade_date"], row["actual_price"]) == (D1.isoformat(), 36_000)
    assert row["next_attempt_at"] == utc_iso(at(D2))
    results = env.judgments.results(jid)
    assert [r["rank"] for r in results] == [1, 2]
    assert env.fake.nonces == [f"j{jid}:0"]
    assert row["announced_at"] is not None
    assert "회사0158S0" in env.fake.calls[0][1]["embeds"][0].title


@pytest.mark.parametrize(("vol", "reason"), [(0, "halted_suspected"), (None, "delisted_suspected")])
async def test_not_traded_five_days_unjudgeable_and_announced(tmp_path, vol, reason):
    def default(market, d):
        rows = published_without_code(market, d)
        if vol is not None and market == "KOSDAQ":
            rows.append(q(d, vol=vol))
        return rows

    e = Env(tmp_path, krx=DatedKrx(default))
    jid = e.add_group()
    await e.tick(at(date(2026, 12, 16)))  # 관측: 10·11·14·15 → 4회
    assert e.row(jid)["status"] == "pending"
    assert e.row(jid)["not_traded_days"] == 4
    await e.tick(at(date(2026, 12, 17)))  # 16일 포함 5회
    row = e.row(jid)
    assert (row["status"], row["last_error"]) == ("unjudgeable", reason)
    assert e.judgments.results(jid) == []
    assert e.fake.nonces == [f"j{jid}:0"]
    assert row["announced_at"] is not None
    e.conn.close()


async def test_same_day_polling_until_published(env):
    jid = env.add_group()
    await env.tick(at(D2))
    row = env.row(jid)
    assert (row["status"], row["last_error"]) == ("pending", "krx_all_empty")
    assert row["next_attempt_at"] == utc_iso(at(D2, 9, 30))
    env.krx.set("KOSDAQ", D1, [q(D1)])
    await env.tick(at(D2, 9, 15))  # 아직 시각 전: 대상 아님
    assert env.row(jid)["status"] == "pending"
    await env.tick(at(D2, 9, 30))
    assert env.row(jid)["status"] == "done"
    assert env.fake.nonces == [f"j{jid}:0"]


async def test_halted_day_then_next_trade_day_adopted(env):
    jid = env.add_group()
    env.krx.set("KOSDAQ", D1, [q(D1, vol=0)])
    env.krx.set("KOSDAQ", D2, [q(D2, close=40_000)])
    await env.tick(at(D3))
    row = env.row(jid)
    assert (row["status"], row["actual_trade_date"], row["not_traded_days"]) == ("done", D2.isoformat(), 1)


# ---------------------------------------------------------------- closed day (R13·F1·ARCH-R3-03)


async def test_override_closed_after_confirmation_adopts_next_day(env):
    jid = env.add_group()
    CalendarRepo(env.conn).set_override(D1, False, "임시 휴장")
    env.krx.set("KOSDAQ", D2, [q(D2)])
    await env.tick(at(D3))
    row = env.row(jid)
    assert (row["status"], row["actual_trade_date"]) == ("done", D2.isoformat())
    assert ("KOSDAQ", D1) not in env.krx.calls


async def test_empty_day_confirmed_by_requery_is_recorded(env, caplog):
    jid = env.add_group()
    env.krx.set("KOSDAQ", D2, [q(D2)])
    with caplog.at_level(logging.WARNING):
        await env.tick(at(D3))
    row = env.row(jid)
    assert (row["status"], row["actual_trade_date"]) == ("done", D2.isoformat())
    assert closed_days(env.conn) == {(D1.isoformat(), D2.isoformat())}
    assert "krx_closed_day_detected" in caplog.text
    # 재조회는 세 시장 모두 캐시 우회
    assert env.krx.calls.count(("KOSPI", D1)) == 2 and env.krx.calls.count(("KONEX", D1)) == 2


async def test_requery_finds_published_day_adopts_it_without_recording(env):
    jid = env.add_group()
    env.krx.script("KOSDAQ", D1, None, [q(D1, close=35_000)])
    env.krx.set("KOSDAQ", D2, [q(D2)])
    await env.tick(at(D3))
    row = env.row(jid)
    assert (row["status"], row["actual_trade_date"], row["actual_price"]) == ("done", D1.isoformat(), 35_000)
    assert closed_days(env.conn) == set()


async def test_requery_fetch_error_waits_without_recording(env):
    jid = env.add_group()
    env.krx.script("KOSDAQ", D1, None, KrxFetchError("503"))
    env.krx.set("KOSDAQ", D2, [q(D2)])
    await env.tick(at(D3))
    row = env.row(jid)
    assert (row["status"], row["last_error"], row["judge_trade_date"]) == ("pending", "krx_fetch", D1.isoformat())
    assert row["next_attempt_at"] == utc_iso(at(D3, 9, 30))
    assert row["unavailable_days"] == 0
    assert closed_days(env.conn) == set()


async def test_two_unpublished_days_stay_pending(env):
    jid = env.add_group()
    await env.tick(at(D3))
    row = env.row(jid)
    assert (row["status"], row["not_traded_days"], row["judge_trade_date"]) == ("pending", 0, D1.isoformat())
    assert closed_days(env.conn) == set()


async def test_calendar_open_but_krx_empty_day_is_skipped(tmp_path):
    labor = date(2026, 5, 1)
    e = Env(tmp_path, cal_cls=LaborOpenCalendar)
    jid = e.add_group(base=date(2026, 4, 1), target=labor)
    e.krx.set("KOSDAQ", date(2026, 5, 4), [q(date(2026, 5, 4))])
    await e.tick(at(date(2026, 5, 5)))  # 어린이날(휴장) 아침
    row = e.row(jid)
    assert (row["status"], row["actual_trade_date"]) == ("done", "2026-05-04")
    assert closed_days(e.conn) == {("2026-05-01", "2026-05-04")}
    assert not e.cal.is_open(labor)
    e.conn.close()


# ---------------------------------------------------------------- 장애·stale (F6·CRIT-P3-F1)


async def test_catch_up_after_long_downtime(env):
    jid = env.add_group()
    env.krx.set("KOSDAQ", D1, KrxFetchError("timeout"))
    await env.tick(at(date(2026, 12, 24), 15, 30))  # 10영업일 다운타임 뒤 첫 시도
    row = env.row(jid)
    assert (row["status"], row["unavailable_days"], row["unavailable_since"]) == ("pending", 1, "2026-12-24")
    assert row["next_attempt_at"] == utc_iso(at(date(2026, 12, 28)))
    env.krx.set("KOSDAQ", D1, [q(D1)])
    await env.tick(at(date(2026, 12, 28)))
    row = env.row(jid)
    assert (row["status"], row["actual_trade_date"], row["unavailable_days"]) == ("done", D1.isoformat(), 0)
    assert row["unavailable_since"] is None


async def test_long_downtime_single_error_is_not_krx_unavailable(env):
    jid = env.add_group()
    env.krx.set("KOSDAQ", D1, KrxFetchError("timeout"))
    await env.tick(at(date(2027, 1, 5), 15, 30))  # 15영업일 넘게 지남
    row = env.row(jid)
    assert (row["status"], row["unavailable_days"]) == ("pending", 1)


async def test_ten_distinct_business_days_of_errors_is_krx_unavailable(tmp_path):
    e = Env(tmp_path, krx=DatedKrx(lambda m, d: KrxFetchError("timeout")))
    jid = e.add_group()
    days = [date(2026, 12, d) for d in (11, 14, 15, 16, 17, 18, 21, 22, 23)]
    for d in days:
        await e.tick(at(d, 15, 30))
        await e.tick(at(d, 15, 45))  # 같은 날 반복은 세지 않는다(next_attempt_at 전이라 대상도 아님)
    row = e.row(jid)
    assert (row["status"], row["unavailable_days"]) == ("pending", 9)
    await e.tick(at(date(2026, 12, 24), 15, 30))
    row = e.row(jid)
    assert (row["status"], row["last_error"]) == ("unjudgeable", "krx_unavailable")
    assert (row["unavailable_since"], row["unavailable_days"]) == ("2026-12-11", 10)
    assert e.fake.nonces == [f"j{jid}:0"]
    e.conn.close()


async def test_auth_errors_never_go_stale_and_alert_once_per_day(tmp_path, caplog):
    e = Env(tmp_path, krx=DatedKrx(lambda m, d: KrxAuthError("401")))
    jid = e.add_group()
    days = []
    d = D2
    while len(days) < 12:
        if e.cal.is_open(d):
            days.append(d)
        d += timedelta(days=1)
    with caplog.at_level(logging.ERROR):
        for d in days:
            await e.tick(at(d, 9, 0))
            await e.tick(at(d, 9, 30))
            await e.tick(at(d, 15, 30))
    row = e.row(jid)
    assert (row["status"], row["unavailable_days"], row["last_error"]) == ("pending", 0, "krx_auth")
    assert caplog.text.count("OPERATOR_ALERT krx_auth") == 12
    e.conn.close()


async def test_data_anomaly_frontier_then_corrected(env):
    jid = env.add_group()
    env.krx.set("KOSDAQ", D1, ([], [RowAnomaly("KOSDAQ", CODE, "MKTCAP 결측")]))
    await env.tick(at(D2))
    row = env.row(jid)
    assert (row["status"], row["last_error"]) == ("pending", "krx_data_anomaly")
    env.krx.set("KOSDAQ", D1, [q(D1)])
    await env.tick(at(D2, 9, 30))
    assert env.row(jid)["status"] == "done"


async def test_group_isolation(env, caplog):
    a = env.add_group(code="111110", market="KOSPI")
    b = env.add_group(code=CODE)
    env.conn.execute(
        "UPDATE judgments SET unavailable_days = 2, unavailable_since = '2026-12-08', "
        "unavailable_last_day = '2026-12-09' WHERE id = ?",
        (a,),
    )
    env.conn.commit()
    env.krx.set("KOSPI", D1, RuntimeError("boom"))
    env.krx.set("KOSDAQ", D1, [q(D1)])
    with caplog.at_level(logging.ERROR):
        await env.tick(at(D2))
    ra, rb = env.row(a), env.row(b)
    assert rb["status"] == "done"
    assert (ra["status"], ra["last_error"]) == ("pending", "judging_error")
    assert ra["next_attempt_at"] == utc_iso(at(D2, 9, 30))
    assert (ra["unavailable_days"], ra["unavailable_since"]) == (2, "2026-12-08")
    assert "판정 시도 실패" in caplog.text


async def test_invalid_actual_is_unjudgeable(env, monkeypatch):
    jid = env.add_group()
    env.krx.set("KOSDAQ", D1, [q(D1)])
    real = judging_mod.decide

    def zero_close(*args, **kwargs):
        dec = real(*args, **kwargs)
        quote = replace(dec.outcome.quote, close_price=0)
        return replace(dec, outcome=judging_policy.Adopt(dec.outcome.day, quote))

    monkeypatch.setattr(judging_mod, "decide", zero_close)
    await env.tick(at(D2))
    row = env.row(jid)
    assert (row["status"], row["last_error"]) == ("unjudgeable", "invalid_actual")
    assert env.fake.nonces == [f"j{jid}:0"]


# ---------------------------------------------------------------- 결과 공지 (R22)


def _two_parts(monkeypatch):
    monkeypatch.setattr(judging_mod, "result_parts", lambda j, r, p: text_parts("가" * 1500 + "\n" + "나" * 1500))


async def test_announce_only_unannounced_terminal_rows(env):
    done = env.add_group()
    pending = env.add_group(code="111110", market="KOSPI")
    env.krx.set("KOSDAQ", D1, [q(D1)])
    await env.tick(at(D2))
    assert env.row(pending)["status"] == "pending"
    assert env.fake.nonces == [f"j{done}:0"]
    await env.tick(at(D2, 9, 30))
    await env.tick(at(D2, 10, 30))
    assert env.fake.nonces == [f"j{done}:0"]  # 완료된 공지는 다시 고르지 않는다


async def test_two_parts_announced_at_only_after_second(env, monkeypatch):
    _two_parts(monkeypatch)
    jid = env.add_group()
    seen = {}

    async def check_before_second():
        r = env.row(jid)
        seen["state"] = (r["announce_parts_sent"], r["announced_at"])

    env.fake.script = [None, check_before_second]
    env.krx.set("KOSDAQ", D1, [q(D1)])
    await env.tick(at(D2))
    assert env.fake.nonces == [f"j{jid}:0", f"j{jid}:1"]
    assert seen["state"] == (1, None)
    row = env.row(jid)
    assert row["announce_parts_sent"] == 2 and row["announced_at"] is not None


async def test_second_part_failure_retries_only_remaining(env, monkeypatch):
    _two_parts(monkeypatch)
    jid = env.add_group()
    env.fake.script = [None, forbidden()]
    env.krx.set("KOSDAQ", D1, [q(D1, vol=0)])  # 5회 정지 → 판정 불가(사유 보존 확인)
    for d in (D1, D2, D3, date(2026, 12, 15), date(2026, 12, 16)):
        env.krx.set("KOSDAQ", d, [q(d, vol=0)])
    await env.tick(at(date(2026, 12, 17)))
    row = env.row(jid)
    assert row["status"] == "unjudgeable"
    assert (row["announce_parts_sent"], row["announce_error"]) == (1, "send_forbidden")
    assert row["last_error"] == "halted_suspected"
    assert row["announced_at"] is None
    assert row["next_attempt_at"] == utc_iso(at(date(2026, 12, 17), 9, 30))
    await env.tick(at(date(2026, 12, 17), 9, 10))  # 백오프 전: 재선택 안 됨
    assert len(env.fake.calls) == 2
    await env.tick(at(date(2026, 12, 17), 9, 30))
    assert env.fake.nonces == [f"j{jid}:0", f"j{jid}:1", f"j{jid}:1"]
    row = env.row(jid)
    assert row["announced_at"] is not None and row["announce_error"] is None


async def test_on_sent_failure_retries_next_tick_without_backoff(env, monkeypatch):
    jid = env.add_group()
    env.krx.set("KOSDAQ", D1, [q(D1)])
    real = env.judgments.mark_part_sent
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db busy")
        return real(*args, **kwargs)

    monkeypatch.setattr(env.svc.judgments, "mark_part_sent", flaky)
    await env.tick(at(D2))
    row = env.row(jid)
    assert (row["announce_parts_sent"], row["announced_at"], row["announce_error"]) == (0, None, None)
    assert row["next_attempt_at"] == utc_iso(at(D2))  # 백오프 없음
    await env.tick(at(D2, 9, 1))
    assert env.fake.nonces == [f"j{jid}:0", f"j{jid}:0"]
    assert len(env.fake.posted) == 1  # 같은 nonce 재시도는 창 안에서 중복 제거
    assert env.row(jid)["announced_at"] is not None


# ---------------------------------------------------------------- 인증 오류 지연 안내 (Part A1·Gate3 F4)


async def test_auth_delay_notice_once_then_result_after_recovery(env):
    jid = env.add_group()
    env.krx.set("KOSDAQ", D1, KrxAuthError("401"))
    await env.tick(at(D2))
    row = env.row(jid)
    assert env.fake.nonces == [f"a{jid}:0"]
    assert "데이터 출처 인증 오류로 결과 발표가 지연" in env.fake.calls[0][1]["content"]
    assert row["auth_delay_notified_at"] is not None and row["auth_delay_parts_sent"] == 1
    assert row["status"] == "pending"
    await env.tick(at(D2, 9, 30))
    assert env.fake.nonces == [f"a{jid}:0"]  # 두 번째 인증 오류 tick: send 증가 없음
    env.krx.set("KOSDAQ", D1, [q(D1)])
    await env.tick(at(D2, 10, 0))
    assert env.row(jid)["status"] == "done"
    assert env.fake.nonces == [f"a{jid}:0", f"j{jid}:0"]


async def test_auth_delay_notice_failure_retried_next_tick(env):
    jid = env.add_group()
    env.fake.script = [forbidden()]
    env.krx.set("KOSDAQ", D1, KrxAuthError("401"))
    await env.tick(at(D2))
    row = env.row(jid)
    assert row["auth_delay_notified_at"] is None and row["status"] == "pending"
    await env.tick(at(D2, 9, 15))  # 백오프 전
    assert len(env.fake.calls) == 1
    await env.tick(at(D2, 9, 30))
    assert env.fake.nonces == [f"a{jid}:0", f"a{jid}:0"]
    assert env.row(jid)["auth_delay_notified_at"] is not None


async def test_adopt_with_auth_seen_sends_only_result(env, monkeypatch):
    """F4: 종결 결정에도 auth_seen이 실릴 수 있다. 그때는 지연 안내 없이 결과만 보낸다."""
    jid = env.add_group()
    env.krx.set("KOSDAQ", D1, [q(D1)])
    env.krx.set("KOSDAQ", D2, KrxAuthError("401"))
    real = judging_mod.decide

    def with_auth(*args, **kwargs):
        return replace(real(*args, **kwargs), auth_seen=True)

    monkeypatch.setattr(judging_mod, "decide", with_auth)
    await env.tick(at(D3))
    assert env.row(jid)["status"] == "done"
    assert env.fake.nonces == [f"j{jid}:0"]
    assert not any(n.startswith("a") for n in env.fake.nonces)
    assert env.row(jid)["auth_delay_notified_at"] is None


async def test_auth_on_later_day_after_traded_is_not_observed(env):
    jid = env.add_group()
    env.krx.set("KOSDAQ", D1, [q(D1)])
    env.krx.set("KOSDAQ", D2, KrxAuthError("401"))
    await env.tick(at(D3))
    assert env.row(jid)["status"] == "done"
    assert env.fake.nonces == [f"j{jid}:0"]
    assert ("KOSDAQ", D2) not in env.krx.calls


def test_nonce_length_fits_max_judgment_id():
    max_id = 2**63 - 1
    for base in (f"a{max_id}", f"j{max_id}"):
        assert len(part_nonce(base, 99)) <= 25
