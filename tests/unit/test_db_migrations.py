import sqlite3

import pytest

from ipobot.db.connection import _migrations, connect, migrate, open_database, transaction, user_version

TABLES = {
    "ipo_listings",
    "stock_master",
    "summaries",
    "krx_market_days",
    "krx_daily_quotes",
    "krx_closed_days",
    "market_calendar_overrides",
    "predictions",
    "prediction_targets",
    "judgments",
    "judgment_results",
    "job_runs",
}


def test_migrate_sets_version_and_is_idempotent(tmp_path):
    conn = connect(tmp_path / "db" / "t.sqlite3")
    assert user_version(conn) == 0
    assert migrate(conn) == 2
    assert user_version(conn) == 2
    assert migrate(conn) == 2  # 두 번째 실행은 아무것도 하지 않는다
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert TABLES <= names


def test_reopen_existing_database(tmp_path):
    path = tmp_path / "t.sqlite3"
    open_database(path).close()
    conn = open_database(path)
    assert user_version(conn) == 2


def test_pragmas(tmp_path):
    conn = open_database(tmp_path / "t.sqlite3")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_part_a_columns_present(tmp_path):
    conn = open_database(tmp_path / "t.sqlite3")
    cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(judgments)")}
    assert cols["auth_delay_notified_at"]["type"] == "TEXT"
    assert cols["auth_delay_notified_at"]["notnull"] == 0
    assert cols["auth_delay_parts_sent"]["type"] == "INTEGER"
    assert cols["auth_delay_parts_sent"]["notnull"] == 1
    assert cols["auth_delay_parts_sent"]["dflt_value"] == "0"
    for name in ("unavailable_since", "unavailable_last_day", "unavailable_days", "announce_parts_sent"):
        assert name in cols
    jr = {r["name"] for r in conn.execute("PRAGMA table_info(job_runs)")}
    assert {"parts_sent", "payload_json", "message_id"} <= jr


def test_foreign_key_enforced(tmp_path):
    conn = open_database(tmp_path / "t.sqlite3")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO prediction_targets VALUES (999, 1, '2026-12-12', 100, 100, '100원', '100원')"
        )


def test_transaction_rolls_back_and_rejects_nesting(tmp_path):
    conn = open_database(tmp_path / "t.sqlite3")
    with pytest.raises(RuntimeError, match="boom"):
        with transaction(conn) as c:
            c.execute("INSERT INTO market_calendar_overrides VALUES ('2026-01-02', 0, NULL)")
            raise RuntimeError("boom")
    assert conn.execute("SELECT count(*) FROM market_calendar_overrides").fetchone()[0] == 0
    assert not conn.in_transaction
    with pytest.raises(RuntimeError, match="중첩"):
        with transaction(conn):
            with transaction(conn):
                pass
    assert not conn.in_transaction


def test_0002_backfills_auth_delay_due_at_from_v1_database(tmp_path):
    # v1 DB에서 last_error='krx_auth'로만 남은 미안내 pending 행은 안내 필요 상태로 이어져야 한다.
    conn = connect(tmp_path / "t.sqlite3")
    (_, v1_sql), *_ = _migrations()
    conn.executescript(v1_sql)
    assert user_version(conn) == 1
    conn.execute(
        "INSERT INTO judgments (stock_code, market, base_date, horizon_months, target_date, judge_trade_date, "
        "status, next_attempt_at, updated_at, last_error) VALUES "
        "('0158S0','KOSDAQ','2026-11-10',1,'2026-12-10','2026-12-10','pending','x','t1','krx_auth'),"
        "('0158S1','KOSDAQ','2026-11-10',1,'2026-12-10','2026-12-10','pending','x','t1','krx_fetch')"
    )
    assert migrate(conn) == 2
    rows = conn.execute("SELECT stock_code, auth_delay_due_at FROM judgments ORDER BY stock_code").fetchall()
    assert [(r[0], r[1]) for r in rows] == [("0158S0", "t1"), ("0158S1", None)]
