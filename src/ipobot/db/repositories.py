"""동기 리포지토리. 메서드마다 트랜잭션 1개로 끝난다(R24). 시각은 ISO-8601 UTC 문자열로 저장한다."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from ipobot.clock import utc_iso
from ipobot.db.connection import transaction
from ipobot.domain.models import DailyQuote


class RepositoryError(Exception):
    pass


class NotFoundError(RepositoryError):
    pass


class PartOrderError(RepositoryError):
    """분할 전송 진행 기록의 순서가 어긋났다(역전·중복 호출)."""


@dataclass(frozen=True)
class NewJudgment:
    stock_code: str
    market: str
    base_date: date
    horizon_months: int
    target_date: date
    judge_trade_date: date
    next_attempt_at: datetime


class JudgmentRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert_pending(self, rows: Sequence[NewJudgment], now: datetime) -> list[int]:
        """pending 판정들을 한 트랜잭션으로 넣는다. 하나라도 실패하면 아무것도 남지 않는다."""
        ids: list[int] = []
        ts = utc_iso(now)
        with transaction(self._conn) as c:
            for r in rows:
                cur = c.execute(
                    """
                    INSERT INTO judgments (stock_code, market, base_date, horizon_months, target_date,
                                           judge_trade_date, status, next_attempt_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        r.stock_code,
                        r.market,
                        r.base_date.isoformat(),
                        r.horizon_months,
                        r.target_date.isoformat(),
                        r.judge_trade_date.isoformat(),
                        utc_iso(r.next_attempt_at),
                        ts,
                    ),
                )
                ids.append(int(cur.lastrowid))
        return ids

    def get(self, judgment_id: int) -> sqlite3.Row:
        row = self._conn.execute("SELECT * FROM judgments WHERE id = ?", (judgment_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"judgment {judgment_id} 없음")
        return row

    def record_done(
        self,
        judgment_id: int,
        actual_trade_date: date,
        actual_price: int,
        actual_market_cap: int,
        now: datetime,
    ) -> None:
        with transaction(self._conn) as c:
            self._require_status(c, judgment_id, "pending")
            c.execute(
                """
                UPDATE judgments
                   SET status = 'done', actual_trade_date = ?, actual_price = ?, actual_market_cap = ?,
                       judge_trade_date = ?, last_error = NULL,
                       unavailable_since = NULL, unavailable_last_day = NULL, unavailable_days = 0,
                       updated_at = ?
                 WHERE id = ?
                """,
                (
                    actual_trade_date.isoformat(),
                    actual_price,
                    actual_market_cap,
                    actual_trade_date.isoformat(),
                    utc_iso(now),
                    judgment_id,
                ),
            )

    def record_unjudgeable(self, judgment_id: int, reason: str, now: datetime) -> None:
        with transaction(self._conn) as c:
            self._require_status(c, judgment_id, "pending")
            c.execute(
                "UPDATE judgments SET status = 'unjudgeable', last_error = ?, updated_at = ? WHERE id = ?",
                (reason, utc_iso(now), judgment_id),
            )

    def mark_part_sent(
        self, judgment_id: int, part_index: int, message_id: str | None, now: datetime
    ) -> None:
        """결과 공지 파트 `part_index` 전송 성공을 기록한다.

        `part_index == announce_parts_sent`일 때만 갱신한다. 0번 파트는 message_id가 필수다.
        """
        with transaction(self._conn) as c:
            row = c.execute(
                "SELECT announce_parts_sent FROM judgments WHERE id = ?", (judgment_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"judgment {judgment_id} 없음")
            sent = row["announce_parts_sent"]
            if part_index != sent:
                raise PartOrderError(
                    f"judgment {judgment_id}: 파트 {part_index} 기록 요청, 진행 카운터는 {sent}"
                )
            if part_index == 0 and not message_id:
                raise ValueError("0번 파트는 message_id가 필요합니다")
            c.execute(
                """
                UPDATE judgments
                   SET announce_parts_sent = announce_parts_sent + 1,
                       announce_message_id = COALESCE(announce_message_id, ?),
                       announce_error = NULL,
                       updated_at = ?
                 WHERE id = ?
                """,
                (message_id, utc_iso(now), judgment_id),
            )

    def mark_announce_failed(self, judgment_id: int, error: str, now: datetime) -> None:
        with transaction(self._conn) as c:
            cur = c.execute(
                "UPDATE judgments SET announce_error = ?, updated_at = ? WHERE id = ?",
                (error, utc_iso(now), judgment_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"judgment {judgment_id} 없음")

    def mark_announced(self, judgment_id: int, total_parts: int, now: datetime) -> None:
        """전 파트 성공 후에만 announced_at을 기록한다."""
        with transaction(self._conn) as c:
            row = c.execute(
                "SELECT announce_parts_sent FROM judgments WHERE id = ?", (judgment_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"judgment {judgment_id} 없음")
            if row["announce_parts_sent"] != total_parts:
                raise PartOrderError(
                    f"judgment {judgment_id}: {total_parts}파트 중 {row['announce_parts_sent']}개만 전송됨"
                )
            c.execute(
                "UPDATE judgments SET announced_at = ?, announce_error = NULL, updated_at = ? WHERE id = ?",
                (utc_iso(now), utc_iso(now), judgment_id),
            )

    def mark_auth_delay_part_sent(self, judgment_id: int, part_index: int, now: datetime) -> None:
        """인증 오류 지연 안내 파트 전송 성공을 기록한다(Part A). 순서 규칙은 mark_part_sent와 같다."""
        with transaction(self._conn) as c:
            row = c.execute(
                "SELECT auth_delay_parts_sent FROM judgments WHERE id = ?", (judgment_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"judgment {judgment_id} 없음")
            sent = row["auth_delay_parts_sent"]
            if part_index != sent:
                raise PartOrderError(
                    f"judgment {judgment_id}: 지연 안내 파트 {part_index} 기록 요청, 진행 카운터는 {sent}"
                )
            c.execute(
                "UPDATE judgments SET auth_delay_parts_sent = auth_delay_parts_sent + 1, updated_at = ? "
                "WHERE id = ?",
                (utc_iso(now), judgment_id),
            )

    def mark_auth_delay_notified(self, judgment_id: int, total_parts: int, now: datetime) -> None:
        """지연 안내 전 파트 성공 후에만 auth_delay_notified_at을 기록한다(1회)."""
        with transaction(self._conn) as c:
            row = c.execute(
                "SELECT auth_delay_parts_sent, auth_delay_notified_at FROM judgments WHERE id = ?",
                (judgment_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"judgment {judgment_id} 없음")
            if row["auth_delay_notified_at"] is not None:
                raise PartOrderError(f"judgment {judgment_id}: 지연 안내는 이미 기록됨")
            if row["auth_delay_parts_sent"] != total_parts:
                raise PartOrderError(
                    f"judgment {judgment_id}: 지연 안내 {total_parts}파트 중 "
                    f"{row['auth_delay_parts_sent']}개만 전송됨"
                )
            c.execute(
                "UPDATE judgments SET auth_delay_notified_at = ?, updated_at = ? WHERE id = ?",
                (utc_iso(now), utc_iso(now), judgment_id),
            )

    @staticmethod
    def _require_status(c: sqlite3.Connection, judgment_id: int, expected: str) -> None:
        row = c.execute("SELECT status FROM judgments WHERE id = ?", (judgment_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"judgment {judgment_id} 없음")
        if row["status"] != expected:
            raise RepositoryError(
                f"judgment {judgment_id}: 상태 {row['status']}, {expected} 필요"
            )


class CalendarRepo:
    """캘린더 계층 데이터: market_calendar_overrides, krx_closed_days."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def overrides(self) -> dict[date, bool]:
        rows = self._conn.execute("SELECT day, is_open FROM market_calendar_overrides").fetchall()
        return {date.fromisoformat(r["day"]): bool(r["is_open"]) for r in rows}

    def closed_days(self) -> set[date]:
        rows = self._conn.execute("SELECT trade_date FROM krx_closed_days").fetchall()
        return {date.fromisoformat(r["trade_date"]) for r in rows}

    def set_override(self, day: date, is_open: bool, note: str | None = None) -> None:
        with transaction(self._conn) as c:
            c.execute(
                "INSERT OR REPLACE INTO market_calendar_overrides (day, is_open, note) VALUES (?, ?, ?)",
                (day.isoformat(), int(is_open), note),
            )

    def add_closed_day(self, trade_date: date, evidence_date: date, now: datetime) -> bool:
        """KRX 관측 휴장을 기록한다. 이미 있으면 False(기존 기록 유지)."""
        with transaction(self._conn) as c:
            cur = c.execute(
                "INSERT INTO krx_closed_days (trade_date, evidence_date, detected_at) "
                "VALUES (?, ?, ?) ON CONFLICT (trade_date) DO NOTHING",
                (trade_date.isoformat(), evidence_date.isoformat(), utc_iso(now)),
            )
            return cur.rowcount == 1


class QuoteCacheRepo:
    """KRX 공개 시장-일자 영구 캐시: krx_market_days + krx_daily_quotes."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def load_day(self, trade_date: date, market: str) -> list[DailyQuote] | None:
        """저장된 시장-일자면 행 목록, 아니면 None."""
        day = self._conn.execute(
            "SELECT 1 FROM krx_market_days WHERE trade_date = ? AND market = ?",
            (trade_date.isoformat(), market),
        ).fetchone()
        if day is None:
            return None
        rows = self._conn.execute(
            "SELECT * FROM krx_daily_quotes WHERE trade_date = ? AND market = ? ORDER BY stock_code",
            (trade_date.isoformat(), market),
        ).fetchall()
        return [
            DailyQuote(
                trade_date=date.fromisoformat(r["trade_date"]),
                market=r["market"],
                stock_code=r["stock_code"],
                corp_name=r["corp_name"],
                close_price=r["close_price"],
                open_price=r["open_price"],
                trade_volume=r["trade_volume"],
                market_cap=r["market_cap"],
                listed_shares=r["listed_shares"],
            )
            for r in rows
        ]

    def save_day(self, trade_date: date, market: str, quotes: Sequence[DailyQuote], now: datetime) -> None:
        """비어 있지 않은 시장-일자를 한 트랜잭션으로 저장한다. 이미 있으면 그대로 둔다."""
        if not quotes:
            raise ValueError("빈 시장-일자는 저장하지 않습니다")
        with transaction(self._conn) as c:
            cur = c.execute(
                "INSERT INTO krx_market_days (trade_date, market, row_count, fetched_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (trade_date, market) DO NOTHING",
                (trade_date.isoformat(), market, len(quotes), utc_iso(now)),
            )
            if cur.rowcount != 1:
                return
            c.executemany(
                "INSERT INTO krx_daily_quotes (trade_date, market, stock_code, corp_name, close_price, "
                "open_price, trade_volume, market_cap, listed_shares) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        q.trade_date.isoformat(),
                        q.market,
                        q.stock_code,
                        q.corp_name,
                        q.close_price,
                        q.open_price,
                        q.trade_volume,
                        q.market_cap,
                        q.listed_shares,
                    )
                    for q in quotes
                ],
            )
