import sqlite3
from datetime import date, datetime

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


def _done(conn):
    repo = JudgmentRepo(conn)
    [jid] = repo.insert_pending([_new()], NOW)
    repo.record_done(jid, date(2026, 12, 10), 25000, 500_000_000_000, NOW)
    return repo, jid


def test_record_done_and_status_guard(conn):
    repo, jid = _done(conn)
    row = repo.get(jid)
    assert (row["status"], row["actual_price"], row["actual_trade_date"]) == ("done", 25000, "2026-12-10")
    with pytest.raises(RepositoryError):
        repo.record_unjudgeable(jid, "x", NOW)


def test_record_done_check_failure_leaves_row_unchanged(conn):
    repo = JudgmentRepo(conn)
    [jid] = repo.insert_pending([_new()], NOW)
    with pytest.raises(sqlite3.IntegrityError):
        repo.record_done(jid, date(2026, 12, 9), 25000, 1, NOW)  # target_date 이전
    assert repo.get(jid)["status"] == "pending"
    assert not conn.in_transaction


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
    repo.mark_announce_failed(jid, "send_ambiguous", NOW)
    assert repo.get(jid)["announce_error"] == "send_ambiguous"
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
    repo.record_done(jid, date(2026, 12, 10), 25000, 500_000_000_000, NOW)
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
