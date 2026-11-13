import sqlite3
from datetime import date, datetime, timedelta
from fractions import Fraction

import pytest

from ipobot.clock import KST
from ipobot.db.connection import open_database
from ipobot.db.repositories import (
    CalendarRepo,
    JudgmentRepo,
    NewJudgment,
    NotFoundError,
    PartOrderError,
    RepositoryError,
)
from ipobot.domain.judging_policy import Adopt, Decision, MarketClosed, Unjudgeable, Wait
from ipobot.domain.models import EMPTY_STREAK, DailyQuote, UnavailStreak
from ipobot.domain.scoring import ScoredEntry

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=KST)


@pytest.fixture
def conn(tmp_path):
    c = open_database(tmp_path / "t.sqlite3")
    yield c
    c.close()


def _insert(conn, table, row):
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))


# ---- 유효한 기본 행 -------------------------------------------------------

JUDGMENT_PENDING = dict(
    stock_code="0158S0",
    market="KOSDAQ",
    base_date="2026-11-10",
    horizon_months=1,
    target_date="2026-12-10",
    judge_trade_date="2026-12-10",
    status="pending",
    next_attempt_at="2026-12-11T00:00:00+00:00",
    updated_at="2026-11-12T00:00:00+00:00",
)
JUDGMENT_DONE = dict(
    JUDGMENT_PENDING,
    status="done",
    actual_trade_date="2026-12-10",
    actual_price=25000,
    actual_market_cap=500_000_000_000,
)
JUDGMENT_ANNOUNCED = dict(
    JUDGMENT_DONE,
    announce_parts_sent=1,
    announce_message_id="123",
    announced_at="2026-12-11T00:01:00+00:00",
)
STREAK = dict(
    unavailable_since="2026-12-10", unavailable_last_day="2026-12-11", unavailable_days=2
)
PREDICTION = dict(
    id=1,
    discord_user_id="u1",
    discord_user_name="tester",
    stock_code="0158S0",
    corp_name="가치토자바이오",
    market="KOSDAQ",
    base_date="2026-11-10",
    base_price=24350,
    base_market_cap=487_000_000_000,
    reason="성장",
    raw_text="...",
    created_at="2026-11-12T00:00:00+00:00",
)
TARGET = dict(
    prediction_id=1,
    horizon_months=1,
    target_date="2026-12-10",
    pred_price=30000,
    pred_market_cap=600_000_000_000,
    price_text="3만원",
    market_cap_text="6천억",
)
RESULT = dict(
    judgment_id=1,
    prediction_id=1,
    price_err_num=1,
    price_err_den=5,
    mcap_err_num=1,
    mcap_err_den=5,
    rank=1,
    is_solo=1,
)
MARKET_DAY = dict(trade_date="2026-11-10", market="KOSDAQ", row_count=3, fetched_at="x")
QUOTE = dict(
    trade_date="2026-11-10",
    market="KOSDAQ",
    stock_code="0158S0",
    corp_name="가치토자바이오",
    close_price=24350,
    open_price=23900,
    trade_volume=1234567,
    market_cap=487_000_000_000,
    listed_shares=20_000_000,
)
JOB_FAILED = dict(job_key="weekly:2026-W46", status="failed", attempts=1, ran_at="x", last_error="dart")
JOB_DONE = dict(
    job_key="weekly:2026-W46",
    status="done",
    attempts=1,
    ran_at="x",
    message_id="m1",
    parts_sent=1,
    payload_json="[]",
)
CLOSED_DAY = dict(trade_date="2026-05-01", evidence_date="2026-05-04", detected_at="x")


def _seed_parents(conn, table):
    if table in ("prediction_targets", "judgment_results"):
        _insert(conn, "predictions", PREDICTION)
    if table == "judgment_results":
        _insert(conn, "judgments", dict(JUDGMENT_DONE, id=1))
    if table == "krx_daily_quotes":
        _insert(conn, "krx_market_days", MARKET_DAY)


VALID = [
    ("judgments", JUDGMENT_PENDING),
    ("judgments", JUDGMENT_DONE),
    ("judgments", JUDGMENT_ANNOUNCED),
    ("judgments", dict(JUDGMENT_PENDING, **STREAK)),
    ("judgments", dict(JUDGMENT_PENDING, unavailable_since="2026-12-10", unavailable_last_day="2026-12-10", unavailable_days=1)),
    ("judgments", dict(JUDGMENT_PENDING, auth_delay_parts_sent=1, auth_delay_notified_at="2026-12-11T00:00:00+00:00")),
    ("judgments", dict(JUDGMENT_DONE, auth_delay_parts_sent=1, auth_delay_notified_at="2026-12-11T00:00:00+00:00")),
    ("judgments", dict(JUDGMENT_PENDING, status="unjudgeable", last_error="krx_unavailable", announce_parts_sent=2, announce_message_id="deleted")),
    ("predictions", PREDICTION),
    ("prediction_targets", TARGET),
    ("judgment_results", RESULT),
    ("krx_market_days", MARKET_DAY),
    ("krx_daily_quotes", QUOTE),
    ("krx_daily_quotes", dict(QUOTE, trade_volume=0, open_price=0)),
    ("job_runs", JOB_FAILED),
    ("job_runs", JOB_DONE),
    ("job_runs", dict(JOB_FAILED, message_id="m1", parts_sent=1, payload_json="[]")),
    ("krx_closed_days", CLOSED_DAY),
]


@pytest.mark.parametrize(("table", "row"), VALID)
def test_valid_rows_accepted(conn, table, row):
    _seed_parents(conn, table)
    _insert(conn, table, row)


VIOLATIONS = [
    # judgments: 상태와 actual_*
    ("done-actual-null", "judgments", dict(JUDGMENT_DONE, actual_price=None)),
    ("done-mcap-null", "judgments", dict(JUDGMENT_DONE, actual_market_cap=None)),
    ("done-trade-date-null", "judgments", dict(JUDGMENT_DONE, actual_trade_date=None)),
    ("done-price-zero", "judgments", dict(JUDGMENT_DONE, actual_price=0)),
    ("done-mcap-zero", "judgments", dict(JUDGMENT_DONE, actual_market_cap=0)),
    ("pending-with-actual", "judgments", dict(JUDGMENT_PENDING, actual_price=100)),
    ("unjudgeable-with-actual", "judgments", dict(JUDGMENT_DONE, status="unjudgeable", last_error="x")),
    ("actual-before-target", "judgments", dict(JUDGMENT_DONE, actual_trade_date="2026-12-09")),
    ("unjudgeable-no-error", "judgments", dict(JUDGMENT_PENDING, status="unjudgeable")),
    ("bad-status", "judgments", dict(JUDGMENT_PENDING, status="waiting")),
    ("bad-market", "judgments", dict(JUDGMENT_PENDING, market="NYSE")),
    ("bad-horizon", "judgments", dict(JUDGMENT_PENDING, horizon_months=2)),
    ("neg-not-traded", "judgments", dict(JUDGMENT_PENDING, not_traded_days=-1)),
    # judgments: 공지 파트 카운터
    ("announced-pending", "judgments", dict(JUDGMENT_PENDING, announced_at="x", announce_parts_sent=1, announce_message_id="1")),
    ("announced-zero-parts", "judgments", dict(JUDGMENT_DONE, announced_at="x")),
    ("message-id-zero-parts", "judgments", dict(JUDGMENT_DONE, announce_message_id="1")),
    ("one-part-no-message-id", "judgments", dict(JUDGMENT_DONE, announce_parts_sent=1)),
    ("pending-parts-sent", "judgments", dict(JUDGMENT_PENDING, announce_parts_sent=1, announce_message_id="1")),
    ("neg-parts-sent", "judgments", dict(JUDGMENT_PENDING, announce_parts_sent=-1)),
    # judgments: streak
    ("streak-days-no-since", "judgments", dict(JUDGMENT_PENDING, unavailable_days=2)),
    ("streak-since-no-days", "judgments", dict(JUDGMENT_PENDING, unavailable_since="2026-12-10", unavailable_last_day="2026-12-10")),
    ("streak-since-no-last", "judgments", dict(JUDGMENT_PENDING, unavailable_since="2026-12-10", unavailable_days=1)),
    ("streak-last-no-since", "judgments", dict(JUDGMENT_PENDING, unavailable_last_day="2026-12-10", unavailable_days=1)),
    ("streak-last-before-since", "judgments", dict(JUDGMENT_PENDING, unavailable_since="2026-12-10", unavailable_last_day="2026-12-09", unavailable_days=1)),
    ("streak-neg-days", "judgments", dict(JUDGMENT_PENDING, unavailable_days=-1)),
    # judgments: Part A
    ("auth-delay-neg", "judgments", dict(JUDGMENT_PENDING, auth_delay_parts_sent=-1)),
    ("auth-delay-null", "judgments", dict(JUDGMENT_PENDING, auth_delay_parts_sent=None)),
    ("auth-delay-notified-zero-parts", "judgments", dict(JUDGMENT_PENDING, auth_delay_notified_at="x")),
    # predictions / targets
    ("base-price-zero", "predictions", dict(PREDICTION, base_price=0)),
    ("base-mcap-neg", "predictions", dict(PREDICTION, base_market_cap=-1)),
    ("code-length", "predictions", dict(PREDICTION, stock_code="12345")),
    ("pred-price-zero", "prediction_targets", dict(TARGET, pred_price=0)),
    ("pred-mcap-zero", "prediction_targets", dict(TARGET, pred_market_cap=0)),
    ("target-horizon-2", "prediction_targets", dict(TARGET, horizon_months=2)),
    # judgment_results
    ("price-den-zero", "judgment_results", dict(RESULT, price_err_den=0)),
    ("mcap-den-zero", "judgment_results", dict(RESULT, mcap_err_den=0)),
    ("err-num-neg", "judgment_results", dict(RESULT, price_err_num=-1)),
    ("rank-zero", "judgment_results", dict(RESULT, rank=0)),
    ("is-solo-2", "judgment_results", dict(RESULT, is_solo=2)),
    # KRX 캐시
    ("trade-volume-neg", "krx_daily_quotes", dict(QUOTE, trade_volume=-1)),
    ("quote-no-market-day", "krx_daily_quotes", dict(QUOTE, trade_date="2026-11-11")),
    ("row-count-zero", "krx_market_days", dict(MARKET_DAY, row_count=0)),
    # job_runs
    ("done-no-message-id", "job_runs", dict(JOB_DONE, message_id=None, parts_sent=0)),
    ("done-zero-parts", "job_runs", dict(JOB_DONE, parts_sent=0)),
    ("done-no-payload", "job_runs", dict(JOB_DONE, payload_json=None)),
    ("failed-no-error", "job_runs", dict(JOB_FAILED, last_error=None)),
    ("parts-one-no-message-id", "job_runs", dict(JOB_FAILED, parts_sent=1, payload_json="[]")),
    ("parts-no-payload", "job_runs", dict(JOB_FAILED, parts_sent=1, message_id="m1")),
    ("attempts-zero", "job_runs", dict(JOB_FAILED, attempts=0)),
    # krx_closed_days
    ("evidence-equal", "krx_closed_days", dict(CLOSED_DAY, evidence_date="2026-05-01")),
    ("evidence-before", "krx_closed_days", dict(CLOSED_DAY, evidence_date="2026-04-30")),
    # 기타
    ("summary-ok-null", "summaries", dict(source_rcept_no="r", corp_code="c", status="ok", created_at="x")),
    ("summary-failed-no-retry", "summaries", dict(source_rcept_no="r", corp_code="c", status="failed", created_at="x")),
    ("override-is-open-2", "market_calendar_overrides", dict(day="2026-01-02", is_open=2)),
    ("master-code-length", "stock_master", dict(stock_code="1234567", corp_name="x", updated_at="x")),
]


@pytest.mark.parametrize(("case", "table", "row"), VIOLATIONS, ids=[v[0] for v in VIOLATIONS])
def test_check_violations(conn, case, table, row):
    _seed_parents(conn, table)
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, table, row)


# ---- JudgmentRepo ----------------------------------------------------------


def _new(horizon=1, code="0158S0", market="KOSDAQ"):
    return NewJudgment(
        stock_code=code,
        market=market,
        base_date=date(2026, 11, 10),
        horizon_months=horizon,
        target_date=date(2026, 12, 10),
        judge_trade_date=date(2026, 12, 10),
        next_attempt_at=datetime(2026, 12, 11, 9, 0, tzinfo=KST),
    )


def test_insert_pending_initial_values(conn):
    repo = JudgmentRepo(conn)
    [jid] = repo.insert_pending([_new()], NOW)
    row = repo.get(jid)
    assert row["status"] == "pending"
    assert row["next_attempt_at"] == "2026-12-11T00:00:00+00:00"
    assert row["unavailable_days"] == 0 and row["unavailable_since"] is None
    assert row["announce_parts_sent"] == 0 and row["announce_message_id"] is None
    assert row["auth_delay_parts_sent"] == 0 and row["auth_delay_notified_at"] is None


def test_insert_pending_rolls_back_whole_batch(conn):
    repo = JudgmentRepo(conn)
    with pytest.raises(sqlite3.IntegrityError):
        repo.insert_pending([_new(1), _new(3), _new(2)], NOW)  # horizon 2는 CHECK 위반
    assert conn.execute("SELECT count(*) FROM judgments").fetchone()[0] == 0
    assert not conn.in_transaction
    # 중복(UNIQUE)도 전체 롤백
    repo.insert_pending([_new(1)], NOW)
    with pytest.raises(sqlite3.IntegrityError):
        repo.insert_pending([_new(3), _new(1)], NOW)
    assert conn.execute("SELECT count(*) FROM judgments").fetchone()[0] == 1


def test_get_missing(conn):
    with pytest.raises(NotFoundError):
        JudgmentRepo(conn).get(42)


D_TARGET = date(2026, 12, 10)
LATER = datetime(2026, 12, 11, 9, 0, tzinfo=KST)


def _quote(d=D_TARGET, close=25000, mcap=500_000_000_000):
    return DailyQuote(d, "KOSDAQ", "0158S0", "가치토자바이오", close, close, 1000, mcap, 20_000_000)


def _decision(outcome, *, day=D_TARGET, closed=(), streak=EMPTY_STREAK, last_error=None, nxt=LATER, nt=0):
    return Decision(outcome, day, nt, tuple(closed), streak, last_error, nxt, False)


def _scored(pid=1, rank=1, solo=True):
    return ScoredEntry(pid, Fraction(1, 7), Fraction(2, 9), rank, solo)


def _done(conn):
    repo = JudgmentRepo(conn)
    [jid] = repo.insert_pending([_new()], NOW)
    _insert(conn, "predictions", PREDICTION)
    repo.apply_decision(jid, _decision(Adopt(D_TARGET, _quote()), nxt=NOW), [_scored()], NOW)
    return repo, jid


def test_apply_decision_adopt_writes_done_results_and_closed_days(conn):
    repo = JudgmentRepo(conn)
    [jid] = repo.insert_pending([_new()], NOW)
    _insert(conn, "predictions", PREDICTION)
    d11 = date(2026, 12, 11)
    closed = [MarketClosed(D_TARGET, d11)]
    repo.apply_decision(jid, _decision(Adopt(d11, _quote(d11)), day=d11, closed=closed, nxt=LATER, nt=1), [_scored()], LATER)
    row = repo.get(jid)
    assert (row["status"], row["actual_trade_date"], row["actual_price"]) == ("done", "2026-12-11", 25000)
    assert row["judge_trade_date"] == "2026-12-11" and row["not_traded_days"] == 1
    assert row["next_attempt_at"] == "2026-12-11T00:00:00+00:00" and row["last_error"] is None
    [res] = repo.results(jid)
    assert (res["price_err_num"], res["price_err_den"], res["mcap_err_num"], res["mcap_err_den"]) == (1, 7, 2, 9)
    assert (res["rank"], res["is_solo"]) == (1, 1)
    assert CalendarRepo(conn).closed_days() == {D_TARGET}
    with pytest.raises(RepositoryError):  # 종결 후 재적용 금지
        repo.apply_decision(jid, _decision(Unjudgeable("halted_suspected"), last_error="halted_suspected"), [], NOW)


def test_apply_decision_wait_updates_streak_and_keeps_pending(conn):
    repo = JudgmentRepo(conn)
    [jid] = repo.insert_pending([_new()], NOW)
    streak = UnavailStreak(D_TARGET, D_TARGET, 1)
    repo.apply_decision(jid, _decision(Wait(D_TARGET), streak=streak, last_error="krx_fetch"), [], NOW)
    row = repo.get(jid)
    assert (row["status"], row["last_error"], row["unavailable_days"]) == ("pending", "krx_fetch", 1)
    assert row["unavailable_since"] == row["unavailable_last_day"] == "2026-12-10"
    assert row["next_attempt_at"] == "2026-12-11T00:00:00+00:00"
    repo.apply_decision(jid, _decision(Unjudgeable("krx_unavailable"), streak=streak, last_error="krx_unavailable"), [], NOW)
    row = repo.get(jid)
    assert (row["status"], row["last_error"], row["unavailable_days"]) == ("unjudgeable", "krx_unavailable", 1)
    assert row["actual_price"] is None


def test_apply_decision_rejects_mismatched_results(conn):
    repo = JudgmentRepo(conn)
    [jid] = repo.insert_pending([_new()], NOW)
    with pytest.raises(ValueError):
        repo.apply_decision(jid, _decision(Adopt(D_TARGET, _quote())), [], NOW)
    with pytest.raises(ValueError):
        repo.apply_decision(jid, _decision(Wait(None)), [_scored()], NOW)
    assert repo.get(jid)["status"] == "pending"


def test_apply_decision_check_failure_rolls_back_everything(conn):
    repo = JudgmentRepo(conn)
    [jid] = repo.insert_pending([_new()], NOW)
    _insert(conn, "predictions", PREDICTION)
    early = date(2026, 12, 9)  # target_date 이전 → done CHECK 위반
    closed = [MarketClosed(date(2026, 12, 8), early)]
    with pytest.raises(sqlite3.IntegrityError):
        repo.apply_decision(jid, _decision(Adopt(early, _quote(early)), day=early, closed=closed), [_scored()], NOW)
    assert repo.get(jid)["status"] == "pending"
    assert repo.results(jid) == []
    assert CalendarRepo(conn).closed_days() == set()
    assert not conn.in_transaction


def test_mark_attempt_failed_keeps_streak(conn):
    repo = JudgmentRepo(conn)
    [jid] = repo.insert_pending([_new()], NOW)
    streak = UnavailStreak(D_TARGET, D_TARGET, 1)
    repo.apply_decision(jid, _decision(Wait(D_TARGET), streak=streak, last_error="krx_fetch"), [], NOW)
    repo.mark_attempt_failed(jid, "judging_error", LATER, NOW)
    row = repo.get(jid)
    assert (row["last_error"], row["unavailable_days"], row["next_attempt_at"]) == (
        "judging_error", 1, "2026-12-11T00:00:00+00:00")


def test_due_queries(conn):
    repo = JudgmentRepo(conn)
    [j1, j3] = repo.insert_pending([_new(1), _new(3)], NOW)
    due_at = datetime.fromisoformat(repo.get(j1)["next_attempt_at"])
    assert repo.due_pending(due_at - timedelta(seconds=1)) == []
    assert {r["id"] for r in repo.due_pending(datetime(2028, 1, 1, tzinfo=KST))} == {j1, j3}
    _insert(conn, "predictions", PREDICTION)
    repo.apply_decision(j1, _decision(Adopt(D_TARGET, _quote()), nxt=NOW), [_scored()], NOW)
    assert [r["id"] for r in repo.due_announcements(NOW)] == [j1]
    assert j1 not in {r["id"] for r in repo.due_pending(datetime(2028, 1, 1, tzinfo=KST))}


def test_mark_part_sent_in_order(conn):
    repo, jid = _done(conn)
    repo.mark_part_sent(jid, 0, "m0", NOW)
    repo.mark_part_sent(jid, 1, "m1", NOW)
    row = repo.get(jid)
    assert row["announce_parts_sent"] == 2
    assert row["announce_message_id"] == "m0"  # 첫 파트 message_id 유지
    repo.mark_announced(jid, 2, NOW)
    assert repo.get(jid)["announced_at"] is not None


def test_mark_part_sent_rejects_out_of_order_and_duplicates(conn):
    repo, jid = _done(conn)
    with pytest.raises(PartOrderError):
        repo.mark_part_sent(jid, 1, "m1", NOW)  # 역전
    repo.mark_part_sent(jid, 0, "m0", NOW)
    with pytest.raises(PartOrderError):
        repo.mark_part_sent(jid, 0, "m0", NOW)  # 중복 호출
    assert repo.get(jid)["announce_parts_sent"] == 1


def test_mark_part_sent_requires_first_message_id(conn):
    repo, jid = _done(conn)
    with pytest.raises(ValueError):
        repo.mark_part_sent(jid, 0, None, NOW)
    assert repo.get(jid)["announce_parts_sent"] == 0


def test_mark_part_sent_on_pending_is_rejected_by_check(conn):
    repo = JudgmentRepo(conn)
    [jid] = repo.insert_pending([_new()], NOW)
    with pytest.raises(sqlite3.IntegrityError):
        repo.mark_part_sent(jid, 0, "m0", NOW)
    assert repo.get(jid)["announce_parts_sent"] == 0


def test_mark_announced_requires_all_parts(conn):
    repo, jid = _done(conn)
    repo.mark_part_sent(jid, 0, "m0", NOW)
    with pytest.raises(PartOrderError):
        repo.mark_announced(jid, 2, NOW)
    assert repo.get(jid)["announced_at"] is None


def test_mark_announce_failed(conn):
    repo, jid = _done(conn)
    repo.mark_announce_failed(jid, "send_ambiguous", LATER, NOW)
    row = repo.get(jid)
    assert row["announce_error"] == "send_ambiguous"
    assert row["next_attempt_at"] == "2026-12-11T00:00:00+00:00"  # 백오프(Gate3 F2)
    assert row["last_error"] is None  # 판정 사유는 건드리지 않는다
    assert repo.due_announcements(NOW) == []
    repo.mark_part_sent(jid, 0, "m0", NOW)
    assert repo.get(jid)["announce_error"] is None


def test_auth_delay_progress_and_single_notification(conn):
    repo = JudgmentRepo(conn)
    [jid] = repo.insert_pending([_new()], NOW)
    with pytest.raises(PartOrderError):
        repo.mark_auth_delay_notified(jid, 1, NOW)  # 파트 미전송
    with pytest.raises(PartOrderError):
        repo.mark_auth_delay_part_sent(jid, 1, NOW)
    repo.mark_auth_delay_part_sent(jid, 0, NOW)
    repo.mark_auth_delay_notified(jid, 1, NOW)
    row = repo.get(jid)
    assert row["status"] == "pending"  # 지연 안내는 pending 유지
    assert row["auth_delay_parts_sent"] == 1
    assert row["auth_delay_notified_at"] == "2026-11-12T00:00:00+00:00"
    with pytest.raises(PartOrderError):
        repo.mark_auth_delay_notified(jid, 1, NOW)
    # 이후 정상 판정이 되어도 값은 보존된다(감사용)
    _insert(conn, "predictions", PREDICTION)
    repo.apply_decision(jid, _decision(Adopt(D_TARGET, _quote())), [_scored()], NOW)
    assert repo.get(jid)["auth_delay_notified_at"] == "2026-11-12T00:00:00+00:00"


# ---- CalendarRepo ----------------------------------------------------------


def test_calendar_repo(conn):
    repo = CalendarRepo(conn)
    repo.set_override(date(2026, 5, 1), True, "오탐 정정")
    repo.set_override(date(2026, 5, 1), False)  # INSERT OR REPLACE
    repo.set_override(date(2026, 6, 4), False)
    assert repo.overrides() == {date(2026, 5, 1): False, date(2026, 6, 4): False}
    assert repo.add_closed_day(date(2026, 7, 1), date(2026, 7, 2), NOW) is True
    assert repo.add_closed_day(date(2026, 7, 1), date(2026, 7, 3), NOW) is False
    assert repo.closed_days() == {date(2026, 7, 1)}
    with pytest.raises(sqlite3.IntegrityError):
        repo.add_closed_day(date(2026, 7, 6), date(2026, 7, 6), NOW)
    assert repo.closed_days() == {date(2026, 7, 1)}
