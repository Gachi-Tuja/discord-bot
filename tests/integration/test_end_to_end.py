"""S16 통합 시나리오: 실제 sqlite 파일 + `bot/app.py`의 `Services` 결합 그대로 + 실제 `SchedulerCog.tick`.

외부 경계만 fake로 둔다: KRX·DART·LLM 클라이언트, 디스코드 채널(S3c autospec fake, 서버 측 nonce 중복 제거).
시계는 주입하고 직접 진행한다. "재시작"은 같은 sqlite 파일로 연결·서비스·cog를 새로 만들고, 디스코드 서버
상태(게시 메시지·nonce 창)는 그대로 둔다. "크래시"는 기록 콜백에서 `BaseException`을 올려 tick을 중간에
끊는 방식이다(스케줄러의 `except Exception`도 통과한다).

KRX fake는 거래일 d의 데이터를 d 다음 날부터 공개한다(실제처럼 장 마감 뒤 공개, 당일 아침에는 없음).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

import pytest

from ipobot.bot.app import Services
from ipobot.bot.cogs.scheduler import SchedulerCog
from ipobot.clock import KST, utc_iso
from ipobot.config import load_settings
from ipobot.db.repositories import JobRunRepo, JudgmentRepo
from ipobot.domain.models import DailyQuote, DartFiling, MarketDayRows
from ipobot.services import alerts
from ipobot.services.ipo_digest import DartUnavailable
from ipobot.services.predictions import Confirmed, Draft
from ipobot.services.quotes import KrxAuthError, KrxFetchError
from ipobot.services.weekly import job_key, week_key
from tests.conftest import REQUIRED_ENV, FakeClock
from tests.fakes.discord_channel import FakeDiscord, forbidden

MARKETS = ("KOSPI", "KOSDAQ", "KONEX")
CODE = "0158S0"  # 영숫자 신형 코드(KOSDAQ)
CODE_B = "111110"  # KOSPI
NAMES = {CODE: "앨리스", CODE_B: "보리"}
CODE_MARKET = {CODE: "KOSDAQ", CODE_B: "KOSPI"}
FILLER = "999990"

THU, FRI, MON = date(2026, 11, 12), date(2026, 11, 13), date(2026, 11, 16)
TARGET_1M = date(2026, 12, 13)  # 기준일(금) + 1개월 = 일요일
JUDGE_DAY = date(2026, 12, 14)  # 목표일 이후 첫 개장일(월)
ANNOUNCE_DAY = date(2026, 12, 15)  # 판정 거래일 다음 영업일 아침(화)
WEEKLY_FRI = date(2026, 12, 18)


def at(d: date, hh: int = 9, mm: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=KST)


class Crash(BaseException):
    """프로세스 강제 종료 흉내. 스케줄러·서비스의 `except Exception`에 잡히지 않는다."""


# ---------------------------------------------------------------- 외부 경계 fake


class Krx:
    """시장·거래일별 일별 시세. 기본은 '전일까지 공개, 대상 종목 정상 거래'.

    - `once(market, d, *values)`: 그 키의 다음 호출들에 차례로 돌려줄 값(소진 후 기본 동작).
    - `sticky[(market, d)]`: 지울 때까지 항상 돌려줄 값.
    - `closed`: 세 시장 모두 빈 응답(임시 휴장). `halted`: 대상 종목 거래량 0.
    값은 None(빈 응답)·예외·DailyQuote 목록.
    """

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.queued: dict[tuple[str, date], list[object]] = {}
        self.sticky: dict[tuple[str, date], object] = {}
        self.closed: set[date] = set()
        self.halted: set[tuple[str, date]] = set()
        self.calls: list[tuple[str, date]] = []

    def once(self, market: str, d: date, *values: object) -> None:
        self.queued.setdefault((market, d), []).extend(values)

    def close_price(self, code: str, d: date) -> int:
        return (36_000 if code == CODE else 52_000) + d.day * 10

    def default(self, market: str, d: date) -> list[DailyQuote] | None:
        if d >= self.clock.now().astimezone(KST).date() or d.weekday() >= 5 or d in self.closed:
            return None
        rows = [DailyQuote(d, market, FILLER, f"채움{market}", 1_000, 1_000, 10, 10**10, 10**7)]
        for code, m in CODE_MARKET.items():
            if m != market:
                continue
            close = self.close_price(code, d)
            vol = 0 if (code, d) in self.halted else 50_000
            rows.append(DailyQuote(d, market, code, NAMES[code], close, close, vol, close * 20_000_000, 20_000_000))
        return rows

    async def fetch_market_day(self, market: str, trade_date: date) -> MarketDayRows:
        self.calls.append((market, trade_date))
        key = (market, trade_date)
        if self.queued.get(key):
            value = self.queued[key].pop(0)
        elif key in self.sticky:
            value = self.sticky[key]
        else:
            value = self.default(market, trade_date)
        if isinstance(value, BaseException):
            raise value
        return MarketDayRows(trade_date, market, tuple(value or ()))


class Dart:
    """증권발행실적보고서 목록·원문. `list_errors`는 list_filings 호출마다 앞에서부터 소비한다."""

    def __init__(self) -> None:
        self.filings: list[DartFiling] = []
        self.listing_dates: dict[str, date] = {}
        self.list_errors: list[Exception] = []
        self.list_calls: list[tuple[date, date]] = []

    def add_listings(self, count: int, listing_date: date) -> None:
        for i in range(count):
            rcept = f"20261201{len(self.filings) + 1:06d}"
            self.filings.append(
                DartFiling(rcept, f"{len(self.filings) + 1:08d}", f"상장사{len(self.filings) + 1:02d}",
                           "증권발행실적보고서", date(2026, 12, 1), market="KOSDAQ")
            )
            self.listing_dates[rcept] = listing_date

    async def list_filings(self, bgn: date, end: date) -> list[DartFiling]:
        self.list_calls.append((bgn, end))
        if self.list_errors:
            raise self.list_errors.pop(0)
        return [f for f in self.filings if bgn <= f.rcept_dt <= end]

    async def document_text(self, rcept_no: str) -> str:
        d = self.listing_dates[rcept_no]
        return f"증권발행실적보고서\n3. 상장일(매매개시일) {d.year}년 {d.month}월 {d.day}일 (예정)\n회사는 반도체 장비를 만든다."

    async def corp_codes(self):
        return []


class Llm:
    """요약과 예측 파싱. 예측 텍스트 형식: '<종목> <1개월 목표가>'."""

    async def summarize(self, corp_name: str, document_text: str) -> str:
        return f"{corp_name}: 반도체 장비 제조사로 코스닥 상장 예정."

    async def parse_prediction(self, text: str) -> dict:
        stock, price = text.split()
        p1 = int(price)
        targets = []
        for h, p, cap_text, cap in (
            (1, p1, "3,150억", 315_000_000_000),
            (3, 30_000, "3,000억", 300_000_000_000),
            (6, 25_000, "2,500억", 250_000_000_000),
            (12, 22_000, "2,200억", 220_000_000_000),
        ):
            targets.append(
                {"horizon_months": h, "price_text": f"{p:,}원", "price": p, "market_cap_text": cap_text, "market_cap": cap}
            )
        return {"stock": stock, "reason": "상장 초기 수급 강세", "targets": targets}


class _ReadyBot:
    async def wait_until_ready(self) -> None:
        return None


# ---------------------------------------------------------------- 하네스


class Harness:
    def __init__(self, tmp_path) -> None:
        self.clock = FakeClock(at(MON, 10))
        self.env = {**REQUIRED_ENV, "DB_PATH": str(tmp_path / "ipobot.sqlite3")}
        self.krx = Krx(self.clock)
        self.dart = Dart()
        self.llm = Llm()
        # 디스코드 서버 상태: 재시작해도 유지된다.
        self.ipo = FakeDiscord(clock=self.clock)
        self.result = FakeDiscord(clock=self.clock)
        self.boots = 0
        self._boot()

    def _boot(self) -> None:
        """`IpoBot.__init__`·`setup_hook`·`scheduler_jobs`와 같은 결합(채널만 fake)."""
        self.boots += 1
        settings = load_settings(self.env)
        alerts.set_default(alerts.OperatorAlerts(self.clock))
        self.svc = Services(settings, self.clock, krx=self.krx, dart=self.dart, llm=self.llm)
        ipo_channel, result_channel = self.ipo.channel(), self.result.channel()

        async def job_factory():
            return self.svc.scheduler_jobs(ipo_channel, result_channel)

        self.cog = SchedulerCog(_ReadyBot(), job_factory, settings.scheduler_tick_seconds)

    async def restart(self) -> None:
        """정상 종료 후 재시작."""
        await self.svc.aclose()
        self._boot()

    def crash_restart(self) -> None:
        """비정상 종료(정리 없음) 후 재시작. 커밋된 상태만 남는다."""
        self.svc.conn.close()
        self._boot()

    async def tick(self, now: datetime) -> None:
        self.clock.set(now)
        await self.cog.tick()

    async def predict(self, user: int, code: str = CODE, price: int = 31_500) -> Confirmed:
        draft = await self.svc.predictions.draft(f"u{user}", f"참가자{user:02d}", f"{code} {price}")
        assert isinstance(draft, Draft), draft
        confirmed = await self.svc.predictions.confirm_draft(draft)
        assert isinstance(confirmed, Confirmed), confirmed
        return confirmed

    def judgment_id(self, code: str = CODE, horizon: int = 1) -> int:
        return self.svc.conn.execute(
            "SELECT id FROM judgments WHERE stock_code = ? AND horizon_months = ?", (code, horizon)
        ).fetchone()["id"]

    def row(self, jid: int):
        return JudgmentRepo(self.svc.conn).get(jid)

    def weekly_row(self, post_date: date):
        return JobRunRepo(self.svc.conn).get(job_key(post_date))

    def close(self) -> None:
        self.svc.conn.close()


def posted(fake: FakeDiscord) -> list[str | None]:
    return [m.nonce for m in fake.posted]


def assert_no_duplicates(*fakes: FakeDiscord) -> None:
    for fake in fakes:
        nonces = posted(fake)
        assert len(nonces) == len(set(nonces)), nonces


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path)
    yield harness
    try:
        harness.close()
    except Exception:
        pass
    alerts.set_default(alerts.OperatorAlerts(alerts.SystemClock()))


# ---------------------------------------------------------------- 1. 등록 → 목표일 → 다음 영업일 아침 공지


async def test_registration_judged_and_announced_next_business_morning(h):
    await h.predict(1, price=31_500)
    await h.predict(2, price=45_000)
    jid = h.judgment_id()
    row = h.row(jid)
    assert (row["base_date"], row["target_date"], row["judge_trade_date"]) == (
        FRI.isoformat(), TARGET_1M.isoformat(), JUDGE_DAY.isoformat()
    )
    assert row["next_attempt_at"] == utc_iso(at(ANNOUNCE_DAY))
    assert h.svc.conn.execute("SELECT COUNT(*) FROM judgments").fetchone()[0] == 4  # 1·3·6·12개월

    await h.tick(at(JUDGE_DAY, 15, 0))  # 목표 거래일 당일: 아직 대상 아님
    await h.tick(at(ANNOUNCE_DAY, 8, 59))
    assert h.result.calls == [] and h.row(jid)["status"] == "pending"

    await h.tick(at(ANNOUNCE_DAY))
    row = h.row(jid)
    assert (row["status"], row["actual_trade_date"]) == ("done", JUDGE_DAY.isoformat())
    assert row["actual_price"] == h.krx.close_price(CODE, JUDGE_DAY)
    assert row["announced_at"] is not None
    assert posted(h.result) == [f"j{jid}:0"]
    embed = h.result.calls[0][1]["embeds"][0]
    assert "앨리스" in embed.title and "1등" in embed.description
    assert "참가자01" in embed.description  # 31,500원이 실제 종가(36,140원)에 더 가깝다
    others = h.svc.conn.execute("SELECT status FROM judgments WHERE horizon_months > 1").fetchall()
    assert {r["status"] for r in others} == {"pending"}
    assert h.ipo.calls == []  # 금요일 게시 창 밖
    await h.tick(at(ANNOUNCE_DAY, 9, 1))
    assert posted(h.result) == [f"j{jid}:0"]


# ---------------------------------------------------------------- 2. 재시작: 대기 판정·미공지 결과·주간 게시


async def test_restart_resumes_pending_unannounced_and_weekly_without_loss(h):
    await h.predict(1)
    await h.predict(2, code=CODE_B, price=60_000)
    ja, jb = h.judgment_id(CODE), h.judgment_id(CODE_B)
    h.dart.add_listings(1, date(2026, 12, 22))
    h.krx.sticky[("KOSPI", JUDGE_DAY)] = KrxFetchError("timeout")  # B는 일시 장애로 대기
    h.result.script = [forbidden()]  # A 결과 공지는 권한 오류로 실패

    await h.tick(at(ANNOUNCE_DAY))
    ra, rb = h.row(ja), h.row(jb)
    assert (ra["status"], ra["announced_at"], ra["announce_error"]) == ("done", None, "send_forbidden")
    assert (rb["status"], rb["last_error"]) == ("pending", "krx_fetch")
    assert posted(h.result) == []

    h.crash_restart()  # 다운: 화요일 09:00 이후 금요일 저녁까지
    del h.krx.sticky[("KOSPI", JUDGE_DAY)]
    await h.tick(at(WEEKLY_FRI, 19, 0))
    assert h.row(ja)["announced_at"] is not None
    rb = h.row(jb)
    assert (rb["status"], rb["actual_trade_date"]) == ("done", JUDGE_DAY.isoformat())
    assert rb["announced_at"] is not None
    assert sorted(posted(h.result)) == sorted([f"j{ja}:0", f"j{jb}:0"])
    assert posted(h.ipo) == [f"w{week_key(WEEKLY_FRI)}:0"]
    assert h.weekly_row(WEEKLY_FRI)["status"] == "done"
    assert "상장사01" in h.ipo.calls[0][1]["embeds"][0].fields[0].name

    await h.restart()
    for now in (at(WEEKLY_FRI, 19, 1), at(WEEKLY_FRI, 19, 30), at(WEEKLY_FRI, 23, 59)):
        await h.tick(now)
    assert len(h.result.posted) == 2 and len(h.ipo.posted) == 1
    assert len(h.dart.list_calls) == 1
    assert_no_duplicates(h.result, h.ipo)


# ---------------------------------------------------------------- 3. 전송 후 기록 전 크래시 → 같은 nonce 중복 제거


async def test_crash_after_send_before_record_dedupes_by_nonce(h, monkeypatch):
    await h.predict(1)
    jid = h.judgment_id()
    real = JudgmentRepo.mark_part_sent
    state = {"crashed": False}

    def crash_before_record(self, *args, **kwargs):
        if not state["crashed"]:
            state["crashed"] = True
            raise Crash
        return real(self, *args, **kwargs)

    monkeypatch.setattr(JudgmentRepo, "mark_part_sent", crash_before_record)
    with pytest.raises(Crash):
        await h.tick(at(ANNOUNCE_DAY))
    assert posted(h.result) == [f"j{jid}:0"]  # 디스코드에는 게시됨
    row = h.row(jid)
    assert (row["status"], row["announce_parts_sent"], row["announced_at"]) == ("done", 0, None)

    h.crash_restart()
    await h.tick(at(ANNOUNCE_DAY, 9, 2))
    assert h.result.nonces == [f"j{jid}:0"]  # 채널 기록에서 기존 게시를 찾아 재전송하지 않음
    assert len(h.result.posted) == 1
    row = h.row(jid)
    assert (row["announce_parts_sent"], row["announced_at"] is not None) == (1, True)
    await h.tick(at(ANNOUNCE_DAY, 9, 30))
    assert len(h.result.calls) == 1


# ---------------------------------------------------------------- 4. 2파트 공지, 0번 기록 뒤 크래시


async def test_two_part_announcement_crash_after_first_part_recorded(h, monkeypatch):
    for user in range(1, 27):  # 26명 → 필드 26개 → 임베드 2개
        await h.predict(user, price=30_000 + user * 100)
    jid = h.judgment_id()
    real = JudgmentRepo.mark_part_sent

    def crash_after_first(self, judgment_id, part_index, *args, **kwargs):
        real(self, judgment_id, part_index, *args, **kwargs)
        if part_index == 0:
            raise Crash

    monkeypatch.setattr(JudgmentRepo, "mark_part_sent", crash_after_first)
    with pytest.raises(Crash):
        await h.tick(at(ANNOUNCE_DAY))
    monkeypatch.undo()
    assert posted(h.result) == [f"j{jid}:0"]
    row = h.row(jid)
    assert (row["announce_parts_sent"], row["announced_at"]) == (1, None)

    h.crash_restart()
    await h.tick(at(ANNOUNCE_DAY, 9, 10))  # 창 밖이어도 0번은 다시 보내지 않는다
    assert h.result.nonces == [f"j{jid}:0", f"j{jid}:1"]
    assert posted(h.result) == [f"j{jid}:0", f"j{jid}:1"]
    assert h.result.calls[1][1]["embeds"][0].title.endswith("(계속)")
    row = h.row(jid)
    assert (row["announce_parts_sent"], row["announced_at"] is not None) == (2, True)
    fields = [f for _, kw in h.result.calls for e in kw["embeds"] for f in e.fields]
    assert len(fields) == 26


# ---------------------------------------------------------------- 5. 2파트 주간 게시, 0번 기록 뒤 크래시


async def test_two_part_weekly_crash_after_first_part_recorded(h, monkeypatch):
    h.dart.add_listings(26, date(2026, 12, 23))  # 26종목 → 임베드 2개
    key = job_key(WEEKLY_FRI)
    real = JobRunRepo.mark_part_sent

    def crash_after_first(self, job, part_index, message_id):
        real(self, job, part_index, message_id)
        if part_index == 0:
            raise Crash

    monkeypatch.setattr(JobRunRepo, "mark_part_sent", crash_after_first)
    with pytest.raises(Crash):
        await h.tick(at(WEEKLY_FRI, 18, 0))
    monkeypatch.undo()
    nonce_base = f"w{week_key(WEEKLY_FRI)}"
    assert posted(h.ipo) == [f"{nonce_base}:0"]
    row = h.weekly_row(WEEKLY_FRI)
    assert (row["status"], row["parts_sent"], row["last_error"]) == ("failed", 1, "sending")

    h.crash_restart()
    await h.tick(at(WEEKLY_FRI, 18, 1))  # 'sending'은 백오프 없이 다음 틱에 남은 파트부터 보낸다
    assert posted(h.ipo) == [f"{nonce_base}:0", f"{nonce_base}:1"]
    row = h.weekly_row(WEEKLY_FRI)
    assert (row["job_key"], row["status"], row["parts_sent"]) == (key, "done", 2)
    assert len(h.dart.list_calls) == 1  # 저장된 payload를 재사용(내용·분할 고정)
    await h.tick(at(WEEKLY_FRI, 18, 45))
    assert len(h.ipo.calls) == 2
    fields = [f for _, kw in h.ipo.calls for e in kw["embeds"] for f in e.fields]
    assert len(fields) == 26


# ---------------------------------------------------------------- 6. 10영업일 넘는 다운 + 일시 오류 1회


async def test_long_downtime_single_transient_error_then_next_business_day(h):
    await h.predict(1)
    jid = h.judgment_id()
    await h.restart()
    h.krx.once("KOSDAQ", JUDGE_DAY, KrxFetchError("timeout"))
    restart_day, next_day = date(2027, 1, 4), date(2027, 1, 5)  # 12/15부터 10영업일 넘게 다운
    await h.tick(at(restart_day, 15, 30))
    row = h.row(jid)
    assert (row["status"], row["last_error"], row["unavailable_days"]) == ("pending", "krx_fetch", 1)
    assert row["next_attempt_at"] == utc_iso(at(next_day))
    assert h.result.calls == []
    await h.tick(at(next_day, 8, 59))
    assert h.result.calls == []
    await h.tick(at(next_day))
    row = h.row(jid)
    assert (row["status"], row["actual_trade_date"], row["unavailable_days"]) == ("done", JUDGE_DAY.isoformat(), 0)
    assert posted(h.result) == [f"j{jid}:0"]


# ---------------------------------------------------------------- 7. DART 장애 주 → 복구 후 1회 게시


async def test_dart_outage_week_fails_then_posts_exactly_once(h):
    h.dart.add_listings(2, date(2026, 12, 24))
    h.dart.list_errors = [DartUnavailable("503"), DartUnavailable("timeout")]
    await h.tick(at(WEEKLY_FRI, 18, 0))
    row = h.weekly_row(WEEKLY_FRI)
    assert (row["status"], row["last_error"], row["attempts"]) == ("failed", "dart_unavailable", 1)
    assert h.ipo.calls == []
    await h.tick(at(WEEKLY_FRI, 18, 10))  # 백오프 전: DART 재조회 안 함
    assert len(h.dart.list_calls) == 1
    await h.tick(at(WEEKLY_FRI, 18, 15))
    assert h.weekly_row(WEEKLY_FRI)["attempts"] == 2 and h.ipo.calls == []
    await h.restart()
    await h.tick(at(WEEKLY_FRI, 18, 30))
    assert posted(h.ipo) == [f"w{week_key(WEEKLY_FRI)}:0"]
    assert h.weekly_row(WEEKLY_FRI)["status"] == "done"
    for now in (at(WEEKLY_FRI, 18, 45), at(WEEKLY_FRI, 21, 0)):
        await h.tick(now)
    assert len(h.ipo.calls) == 1 and len(h.dart.list_calls) == 3


# ---------------------------------------------------------------- 8. 임시 휴장(빈 응답) 통과 후 판정


async def test_temporary_closure_day_is_skipped_and_judging_proceeds(h):
    await h.predict(1)
    jid = h.judgment_id()
    h.krx.closed.add(JUDGE_DAY)  # 캘린더상 개장일이지만 세 시장 모두 빈 응답
    await h.tick(at(ANNOUNCE_DAY))
    assert h.row(jid)["status"] == "pending"  # 다음 날 공개 전에는 휴장을 확정할 수 없다
    assert h.result.calls == []
    next_day = date(2026, 12, 16)
    await h.tick(at(next_day))
    row = h.row(jid)
    assert (row["status"], row["actual_trade_date"]) == ("done", ANNOUNCE_DAY.isoformat())
    closed = h.svc.conn.execute("SELECT trade_date, evidence_date FROM krx_closed_days").fetchall()
    assert [(r["trade_date"], r["evidence_date"]) for r in closed] == [
        (JUDGE_DAY.isoformat(), ANNOUNCE_DAY.isoformat())
    ]
    assert posted(h.result) == [f"j{jid}:0"]
    assert "다음 거래일 적용" in h.result.calls[0][1]["embeds"][0].description


# ---------------------------------------------------------------- Part A1: 인증 오류 지연 안내


async def test_auth_delay_notice_once_across_restart_then_result(h, caplog):
    await h.predict(1)
    jid = h.judgment_id()
    h.krx.sticky[("KOSDAQ", JUDGE_DAY)] = KrxAuthError("401")

    def alert_count() -> int:
        return sum("OPERATOR_ALERT krx_auth" in r.getMessage() for r in caplog.records)

    with caplog.at_level(logging.ERROR):
        await h.tick(at(ANNOUNCE_DAY))
        assert posted(h.result) == [f"a{jid}:0"]
        assert h.result.calls[0][1]["content"] == (
            "⏳ 앨리스 예측 결과: 데이터 출처 인증 오류로 결과 발표가 지연되고 있습니다. 복구되면 결과를 공지합니다."
        )
        row = h.row(jid)
        assert (row["status"], row["last_error"], row["auth_delay_parts_sent"]) == ("pending", "krx_auth", 1)
        assert row["auth_delay_notified_at"] is not None
        assert alert_count() == 1

        await h.tick(at(ANNOUNCE_DAY, 9, 30))  # 같은 날 두 번째 인증 오류: 안내·경보 반복 없음
        assert len(h.result.calls) == 1 and alert_count() == 1

        h.crash_restart()  # 메모리의 안내 예약은 사라져도 DB 기록으로 재안내하지 않는다
        await h.tick(at(ANNOUNCE_DAY, 10, 0))
        assert len(h.result.calls) == 1
        assert h.row(jid)["unavailable_days"] == 0  # 인증 오류는 stale로 세지 않는다

        await h.tick(at(date(2026, 12, 16)))  # 다음 영업일: 경보는 다시 1회
        assert len(h.result.calls) == 1
        before = alert_count()
        await h.tick(at(date(2026, 12, 16), 9, 30))
        assert alert_count() == before

    del h.krx.sticky[("KOSDAQ", JUDGE_DAY)]
    await h.tick(at(date(2026, 12, 16), 10, 0))
    row = h.row(jid)
    assert (row["status"], row["actual_trade_date"]) == ("done", JUDGE_DAY.isoformat())
    assert posted(h.result) == [f"a{jid}:0", f"j{jid}:0"]
    assert row["announced_at"] is not None


async def test_auth_delay_notice_crash_after_send_before_record_is_resent_once(h, monkeypatch):
    # 안내를 게시한 뒤 진행 기록 전에 죽으면 메모리 대기열이 사라진다. 재시작 후 DB에서 되살려
    # 다음 틱(+1분, nonce 중복 제거 창 안)에 같은 nonce로 재전송하고 메시지는 하나로 남아야 한다.
    await h.predict(1)
    jid = h.judgment_id()
    h.krx.sticky[("KOSDAQ", JUDGE_DAY)] = KrxAuthError("401")

    def crash(self, judgment_id, part_index, now):
        raise Crash

    monkeypatch.setattr(JudgmentRepo, "mark_auth_delay_part_sent", crash)
    with pytest.raises(Crash):
        await h.tick(at(ANNOUNCE_DAY))
    monkeypatch.undo()
    assert posted(h.result) == [f"a{jid}:0"]
    assert (h.row(jid)["auth_delay_parts_sent"], h.row(jid)["auth_delay_notified_at"]) == (0, None)

    h.crash_restart()
    await h.tick(at(ANNOUNCE_DAY, 9, 1))
    row = h.row(jid)
    assert row["auth_delay_notified_at"] is not None and row["auth_delay_parts_sent"] == 1
    assert posted(h.result) == [f"a{jid}:0"]
    assert [nonce for nonce, _ in h.result.calls] == [f"a{jid}:0"]  # 기존 게시를 찾아 재전송 생략
    await h.tick(at(ANNOUNCE_DAY, 9, 30))
    assert len(h.result.calls) == 1


async def test_auth_delay_notice_crash_before_send_is_sent_after_restart(h, monkeypatch):
    # 판정(Wait·krx_auth)은 기록됐지만 안내 전에 죽은 경우: 재시작한 인스턴스가 DB에서 대기열을 되살린다.
    await h.predict(1)
    jid = h.judgment_id()
    h.krx.sticky[("KOSDAQ", JUDGE_DAY)] = KrxAuthError("401")

    async def crash(self):
        raise Crash

    from ipobot.services.judging import JudgingService

    monkeypatch.setattr(JudgingService, "run_announcements", crash)
    with pytest.raises(Crash):
        await h.tick(at(ANNOUNCE_DAY))
    monkeypatch.undo()
    assert h.result.calls == [] and h.row(jid)["last_error"] == "krx_auth"

    h.crash_restart()
    await h.tick(at(ANNOUNCE_DAY, 9, 1))
    assert posted(h.result) == [f"a{jid}:0"]
    assert h.row(jid)["auth_delay_notified_at"] is not None


# ---------------------------------------------------------------- 판정 불가 경로


async def test_halted_five_days_is_unjudgeable_and_announced(h):
    await h.predict(1)
    await h.predict(2, price=35_000)
    jid = h.judgment_id()
    for d in (JUDGE_DAY, ANNOUNCE_DAY, date(2026, 12, 16), date(2026, 12, 17), date(2026, 12, 18)):
        h.krx.halted.add((CODE, d))
    await h.tick(at(date(2026, 12, 18)))
    row = h.row(jid)
    assert (row["status"], row["not_traded_days"]) == ("pending", 4)
    assert h.result.calls == []
    await h.tick(at(date(2026, 12, 21)))
    row = h.row(jid)
    assert (row["status"], row["last_error"]) == ("unjudgeable", "halted_suspected")
    assert JudgmentRepo(h.svc.conn).results(jid) == []
    assert posted(h.result) == [f"j{jid}:0"]
    embed = h.result.calls[0][1]["embeds"][0]
    assert "판정 불가" in embed.title
    assert [f.name for f in embed.fields] == ["참가자01", "참가자02"]


async def test_ten_business_days_of_errors_is_unjudgeable_krx_unavailable(h):
    await h.predict(1)
    jid = h.judgment_id()
    h.krx.sticky[("KOSDAQ", JUDGE_DAY)] = KrxFetchError("timeout")
    day = ANNOUNCE_DAY
    for _ in range(9):
        await h.tick(at(day, 15, 30))
        day = day + timedelta(days=3 if day.weekday() == 4 else 1)
        if day == date(2026, 12, 25):
            day = date(2026, 12, 28)
    row = h.row(jid)
    assert (row["status"], row["unavailable_days"]) == ("pending", 9)
    assert h.result.calls == []
    await h.tick(at(day, 15, 30))
    row = h.row(jid)
    assert (row["status"], row["last_error"]) == ("unjudgeable", "krx_unavailable")
    assert posted(h.result) == [f"j{jid}:0"]
    assert "영업일" in h.result.calls[0][1]["embeds"][0].description
