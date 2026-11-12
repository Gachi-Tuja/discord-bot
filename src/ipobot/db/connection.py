"""단일 SQLite 연결과 마이그레이션(§5 동시성 모델, R24).

연결은 autocommit 모드(isolation_level=None)로 열고, 트랜잭션은 `transaction()`이
명시적으로 BEGIN IMMEDIATE / COMMIT / ROLLBACK 한다. 리포지토리 메서드는 이 컨텍스트
하나로 끝나며 그 안에서 await하지 않는다.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

BUSY_TIMEOUT_MS = 5000
_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def connect(db_path: str | Path) -> sqlite3.Connection:
    """연결을 열고 WAL·foreign_keys·busy_timeout을 켠다. 마이그레이션은 하지 않는다."""
    path = str(db_path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """트랜잭션 1개. 예외가 나면 롤백하고 다시 던진다."""
    if conn.in_transaction:
        raise RuntimeError("중첩 트랜잭션은 허용하지 않습니다")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _migrations() -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for entry in resources.files("ipobot.db.migrations").iterdir():
        m = _MIGRATION_RE.match(entry.name)
        if m:
            found.append((int(m.group(1)), entry.read_text(encoding="utf-8")))
    found.sort()
    for expected, (version, _) in enumerate(found, start=1):
        if version != expected:
            raise RuntimeError(f"마이그레이션 번호가 연속되지 않습니다: {version}")
    return found


def user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> int:
    """user_version보다 큰 마이그레이션을 순서대로 적용한다. 멱등이다. 최종 버전을 반환한다."""
    current = user_version(conn)
    for version, sql in _migrations():
        if version <= current:
            continue
        # executescript는 대기 중인 트랜잭션을 먼저 커밋하므로 BEGIN/COMMIT을 스크립트에 넣는다.
        try:
            conn.executescript(f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        current = version
    return current


def open_database(db_path: str | Path) -> sqlite3.Connection:
    conn = connect(db_path)
    migrate(conn)
    return conn
