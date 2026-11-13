"""동기 리포지토리. 메서드마다 트랜잭션 1개로 끝난다(R24). 시각은 ISO-8601 UTC 문자열로 저장한다."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from ipobot.clock import utc_iso
from ipobot.db.connection import transaction
from ipobot.domain.judging_policy import LAST_ERROR_BY_FETCH_KIND, Adopt, Decision, Unjudgeable
from ipobot.domain.models import DailyQuote, IpoListing, PredictionTarget, StockMasterEntry, SummaryRecord
from ipobot.domain.scoring import ScoredEntry


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

    def due_pending(self, now: datetime) -> list[sqlite3.Row]:
        """판정 시도 대상(R13): pending이고 next_attempt_at ≤ now."""
        return self._conn.execute(
            "SELECT * FROM judgments WHERE status = 'pending' AND next_attempt_at <= ? ORDER BY next_attempt_at, id",
            (utc_iso(now),),
        ).fetchall()

    def pending_auth_notice_ids(self) -> list[int]:
        """지연 안내가 남은 판정(Part A1): pending·미안내이면서 마지막 시도가 인증 오류였거나 안내를 일부 보낸 행.

        재시작 후 메모리 대기열을 DB에서 되살리는 데 쓴다.
        """
        rows = self._conn.execute(
            """
            SELECT id FROM judgments
             WHERE status = 'pending' AND auth_delay_notified_at IS NULL
               AND (last_error = ? OR auth_delay_parts_sent > 0)
             ORDER BY id
            """,
            (LAST_ERROR_BY_FETCH_KIND["auth"],),
        ).fetchall()
        return [row["id"] for row in rows]

    def due_announcements(self, now: datetime) -> list[sqlite3.Row]:
        """공지 대상(R22 2단계): 종결·미공지이고 next_attempt_at ≤ now."""
        return self._conn.execute(
            """
            SELECT * FROM judgments
             WHERE status IN ('done','unjudgeable') AND announced_at IS NULL AND next_attempt_at <= ?
             ORDER BY next_attempt_at, id
            """,
            (utc_iso(now),),
        ).fetchall()

    def results(self, judgment_id: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM judgment_results WHERE judgment_id = ? ORDER BY rank, prediction_id",
            (judgment_id,),
        ).fetchall()

    def apply_decision(
        self,
        judgment_id: int,
        decision: Decision,
        results: Sequence[ScoredEntry],
        now: datetime,
    ) -> None:
        """판정 시도 결과를 트랜잭션 1개로 저장한다(R13 출력과 저장 2·3단계, Gate3 F3).

        - `decision.closed_days`를 krx_closed_days에 기록한다(이미 있으면 유지). 서비스가 재조회로 확정한 날짜만 넘긴다.
        - judge_trade_date·not_traded_days·unavailable_*·last_error·next_attempt_at을 갱신한다.
        - Adopt면 status='done'·actual_*·judgment_results, Unjudgeable이면 status='unjudgeable'.
          종결 전이의 next_attempt_at은 decide()가 now로 둔다(CRIT-P3-F5).
        """
        outcome = decision.outcome
        if isinstance(outcome, Adopt):
            if not results:
                raise ValueError("done 전이에는 채점 결과가 필요합니다")
            status = "done"
            actual = (outcome.day.isoformat(), outcome.quote.close_price, outcome.quote.market_cap)
        else:
            if results:
                raise ValueError("done이 아닌 전이에는 채점 결과를 저장할 수 없습니다")
            status = "unjudgeable" if isinstance(outcome, Unjudgeable) else "pending"
            actual = (None, None, None)
        streak = decision.streak
        ts = utc_iso(now)
        with transaction(self._conn) as c:
            self._require_status(c, judgment_id, "pending")
            for mc in decision.closed_days:
                c.execute(
                    "INSERT INTO krx_closed_days (trade_date, evidence_date, detected_at) "
                    "VALUES (?, ?, ?) ON CONFLICT (trade_date) DO NOTHING",
                    (mc.day.isoformat(), mc.evidence.isoformat(), ts),
                )
            c.execute(
                """
                UPDATE judgments
                   SET status = ?, judge_trade_date = ?, not_traded_days = ?,
                       unavailable_since = ?, unavailable_last_day = ?, unavailable_days = ?,
                       last_error = ?, next_attempt_at = ?,
                       actual_trade_date = ?, actual_price = ?, actual_market_cap = ?,
                       updated_at = ?
                 WHERE id = ?
                """,
                (
                    status,
                    decision.judge_trade_date.isoformat(),
                    decision.not_traded_days,
                    streak.since.isoformat() if streak.since else None,
                    streak.last_day.isoformat() if streak.last_day else None,
                    streak.days,
                    decision.last_error,
                    utc_iso(decision.next_attempt_at),
                    *actual,
                    ts,
                    judgment_id,
                ),
            )
            c.executemany(
                """
                INSERT INTO judgment_results
                  (judgment_id, prediction_id, price_err_num, price_err_den, mcap_err_num, mcap_err_den, rank, is_solo)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (judgment_id, r.prediction_id, *r.price_err_parts, *r.mcap_err_parts, r.rank, int(r.is_solo))
                    for r in results
                ],
            )

    def mark_attempt_failed(self, judgment_id: int, error: str, next_attempt_at: datetime, now: datetime) -> None:
        """판정 시도 중 예외(그룹 격리, R23): last_error·next_attempt_at만 바꾸고 streak은 그대로 둔다."""
        with transaction(self._conn) as c:
            cur = c.execute(
                "UPDATE judgments SET last_error = ?, next_attempt_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (error, utc_iso(next_attempt_at), utc_iso(now), judgment_id),
            )
            if cur.rowcount == 0:
                raise RepositoryError(f"judgment {judgment_id}: pending 행이 아닙니다")

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

    def mark_announce_failed(
        self, judgment_id: int, error: str, next_attempt_at: datetime, now: datetime
    ) -> None:
        """공지 전송 실패(R22 4단계, Gate3 F2): announce_error와 재시도 시각을 기록한다. last_error(판정 사유)는 그대로다."""
        with transaction(self._conn) as c:
            cur = c.execute(
                """
                UPDATE judgments SET announce_error = ?, next_attempt_at = ?, updated_at = ?
                 WHERE id = ? AND status IN ('done','unjudgeable') AND announced_at IS NULL
                """,
                (error, utc_iso(next_attempt_at), utc_iso(now), judgment_id),
            )
            if cur.rowcount == 0:
                raise RepositoryError(f"judgment {judgment_id}: 공지 대기 중인 종결 행이 아닙니다")

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


class DuplicatePredictionError(RepositoryError):
    """같은 사용자·종목·기준일 예측이 이미 있다(R5)."""


class GroupClosedError(RepositoryError):
    """그룹 중 하나 이상이 이미 판정(done·unjudgeable)되었다(R12, U-GROUP)."""


@dataclass(frozen=True)
class NewPrediction:
    discord_user_id: str
    discord_user_name: str
    stock_code: str
    corp_name: str
    market: str
    base_date: date
    base_price: int
    base_market_cap: int
    reason: str
    raw_text: str
    targets: tuple[tuple[PredictionTarget, date], ...]  # (예측, 목표일), 1·3·6·12 순서


class PredictionRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def confirm(self, p: NewPrediction, judgments: Sequence[NewJudgment], now: datetime) -> int:
        """R8: predictions + targets + judgments upsert를 트랜잭션 1개로 저장하고 예측 id를 돌려준다.

        중복이면 DuplicatePredictionError, 그룹에 pending이 아닌 판정이 있으면 GroupClosedError.
        어느 경우든 예외면 전부 롤백된다.
        """
        ts = utc_iso(now)
        base = p.base_date.isoformat()
        with transaction(self._conn) as c:
            try:
                cur = c.execute(
                    """
                    INSERT INTO predictions (discord_user_id, discord_user_name, stock_code, corp_name, market,
                                             base_date, base_price, base_market_cap, reason, raw_text, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        p.discord_user_id,
                        p.discord_user_name,
                        p.stock_code,
                        p.corp_name,
                        p.market,
                        base,
                        p.base_price,
                        p.base_market_cap,
                        p.reason,
                        p.raw_text,
                        ts,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "UNIQUE" in str(exc) and "predictions." in str(exc):
                    raise DuplicatePredictionError(
                        f"예측 중복 user={p.discord_user_id} code={p.stock_code} base={base}"
                    ) from exc
                raise
            prediction_id = int(cur.lastrowid)
            for target, target_date in p.targets:
                c.execute(
                    """
                    INSERT INTO prediction_targets (prediction_id, horizon_months, target_date, pred_price,
                                                    pred_market_cap, price_text, market_cap_text)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        prediction_id,
                        target.horizon_months,
                        target_date.isoformat(),
                        target.pred_price,
                        target.pred_market_cap,
                        target.price_text,
                        target.market_cap_text,
                    ),
                )
            for j in judgments:
                c.execute(
                    """
                    INSERT INTO judgments (stock_code, market, base_date, horizon_months, target_date,
                                           judge_trade_date, status, next_attempt_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    ON CONFLICT (stock_code, base_date, horizon_months) DO NOTHING
                    """,
                    (
                        j.stock_code,
                        j.market,
                        j.base_date.isoformat(),
                        j.horizon_months,
                        j.target_date.isoformat(),
                        j.judge_trade_date.isoformat(),
                        utc_iso(j.next_attempt_at),
                        ts,
                    ),
                )
            closed = c.execute(
                "SELECT horizon_months, status FROM judgments "
                "WHERE stock_code = ? AND base_date = ? AND status <> 'pending' ORDER BY horizon_months",
                (p.stock_code, base),
            ).fetchall()
            if closed:
                raise GroupClosedError(
                    f"그룹 {p.stock_code}/{base} 판정됨: "
                    + ", ".join(f"{r['horizon_months']}M={r['status']}" for r in closed)
                )
        return prediction_id

    def exists(self, discord_user_id: str, stock_code: str, base_date: date) -> bool:
        """R5 사전 검사. 최종 판단은 confirm의 UNIQUE다."""
        row = self._conn.execute(
            "SELECT 1 FROM predictions WHERE discord_user_id = ? AND stock_code = ? AND base_date = ?",
            (discord_user_id, stock_code, base_date.isoformat()),
        ).fetchone()
        return row is not None

    def group_closed(self, stock_code: str, base_date: date) -> bool:
        """R12 사전 검사: 그룹에 pending이 아닌 판정 행이 있는가. 최종 판단은 confirm이다."""
        row = self._conn.execute(
            "SELECT 1 FROM judgments WHERE stock_code = ? AND base_date = ? AND status <> 'pending' LIMIT 1",
            (stock_code, base_date.isoformat()),
        ).fetchone()
        return row is not None

    def group_predictions(self, judgment_id: int) -> list[sqlite3.Row]:
        """판정 행의 그룹 예측(R12): id, discord_user_name, corp_name, pred_price, pred_market_cap."""
        return self._conn.execute(
            """
            SELECT p.id, p.discord_user_id, p.discord_user_name, p.corp_name, t.pred_price, t.pred_market_cap
              FROM judgments j
              JOIN predictions p ON p.stock_code = j.stock_code AND p.base_date = j.base_date
              JOIN prediction_targets t ON t.prediction_id = p.id AND t.horizon_months = j.horizon_months
             WHERE j.id = ?
             ORDER BY p.id
            """,
            (judgment_id,),
        ).fetchall()


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


class StockMasterRepo:
    """corpCode.xml 기반 종목 마스터(stock_master)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def upsert_all(self, entries: Sequence[StockMasterEntry], now: datetime) -> int:
        """전체 목록을 트랜잭션 1회로 upsert한다. 반환값은 처리한 행 수."""
        stamp = utc_iso(now)
        with transaction(self._conn) as c:
            c.executemany(
                "INSERT INTO stock_master (stock_code, corp_code, corp_name, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (stock_code) DO UPDATE SET corp_code = excluded.corp_code, "
                "corp_name = excluded.corp_name, updated_at = excluded.updated_at",
                [(e.stock_code, e.corp_code, e.corp_name, stamp) for e in entries],
            )
        return len(entries)

    def stock_code_for_corp(self, corp_code: str) -> str | None:
        row = self._conn.execute(
            "SELECT stock_code FROM stock_master WHERE corp_code = ? ORDER BY stock_code LIMIT 1", (corp_code,)
        ).fetchone()
        return None if row is None else row["stock_code"]

    def codes_for_name(self, corp_name: str) -> list[str]:
        """corp_name이 정확히 같은 종목코드 목록(R7 2단계)."""
        rows = self._conn.execute(
            "SELECT stock_code FROM stock_master WHERE corp_name = ? ORDER BY stock_code", (corp_name,)
        ).fetchall()
        return [r["stock_code"] for r in rows]


class IpoListingRepo:
    """상장 예정 종목(ipo_listings). DART 원문 추출 결과를 접수번호 단위로 캐시한다."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def by_rcept_no(self, rcept_no: str) -> IpoListing | None:
        row = self._conn.execute(
            "SELECT * FROM ipo_listings WHERE source_rcept_no = ? ORDER BY id DESC LIMIT 1", (rcept_no,)
        ).fetchone()
        return None if row is None else self._from_row(row)

    def market_for_code(self, stock_code: str) -> str | None:
        """종목코드의 상장 시장(가장 최근 상장일 기준). 모르면 None(R1 힌트)."""
        row = self._conn.execute(
            "SELECT market FROM ipo_listings WHERE stock_code = ? AND market IS NOT NULL "
            "ORDER BY listing_date DESC, id DESC LIMIT 1",
            (stock_code,),
        ).fetchone()
        return None if row is None else row["market"]

    def upsert_many(self, listings: Sequence[IpoListing], now: datetime) -> None:
        """트랜잭션 1회로 저장한다. 같은 회사의 다른 상장일 행(정정 전 일정)은 지운다."""
        stamp = utc_iso(now)
        with transaction(self._conn) as c:
            for it in listings:
                day = it.listing_date.isoformat()
                c.execute(
                    "DELETE FROM ipo_listings WHERE corp_code = ? AND listing_date <> ?", (it.corp_code, day)
                )
                c.execute(
                    "INSERT INTO ipo_listings (corp_code, corp_name, stock_code, market, listing_date, "
                    "source_rcept_no, collected_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (corp_code, listing_date) DO UPDATE SET corp_name = excluded.corp_name, "
                    "stock_code = COALESCE(excluded.stock_code, ipo_listings.stock_code), "
                    "market = COALESCE(excluded.market, ipo_listings.market), "
                    "source_rcept_no = excluded.source_rcept_no, collected_at = excluded.collected_at",
                    (it.corp_code, it.corp_name, it.stock_code, it.market, day, it.source_rcept_no, stamp),
                )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> IpoListing:
        return IpoListing(
            corp_code=row["corp_code"],
            corp_name=row["corp_name"],
            listing_date=date.fromisoformat(row["listing_date"]),
            source_rcept_no=row["source_rcept_no"],
            stock_code=row["stock_code"],
            market=row["market"],
        )


class SummaryRepo:
    """DART 요약 캐시(summaries, R14). 접수번호당 1행이며 재시도 결과로 덮어쓴다."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, rcept_no: str) -> SummaryRecord | None:
        row = self._conn.execute("SELECT * FROM summaries WHERE source_rcept_no = ?", (rcept_no,)).fetchone()
        if row is None:
            return None
        retry = row["retry_after"]
        return SummaryRecord(
            source_rcept_no=row["source_rcept_no"],
            corp_code=row["corp_code"],
            status=row["status"],
            summary=row["summary"],
            retry_after=None if retry is None else datetime.fromisoformat(retry),
        )

    def save_ok(self, rcept_no: str, corp_code: str, summary: str, now: datetime) -> None:
        self._upsert(rcept_no, corp_code, "ok", summary, None, now)

    def save_failed(self, rcept_no: str, corp_code: str, retry_after: datetime, now: datetime) -> None:
        self._upsert(rcept_no, corp_code, "failed", None, utc_iso(retry_after), now)

    def _upsert(
        self, rcept_no: str, corp_code: str, status: str, summary: str | None, retry_after: str | None, now: datetime
    ) -> None:
        with transaction(self._conn) as c:
            c.execute(
                "INSERT INTO summaries (source_rcept_no, corp_code, status, summary, retry_after, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (source_rcept_no) DO UPDATE SET "
                "corp_code = excluded.corp_code, status = excluded.status, summary = excluded.summary, "
                "retry_after = excluded.retry_after, created_at = excluded.created_at",
                (rcept_no, corp_code, status, summary, retry_after, utc_iso(now)),
            )


class JobRunRepo:
    """`job_runs`(R21). done = 전 파트 전송 성공, failed = 수집·전송 실패 또는 전송 중(선기록)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, job_key: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM job_runs WHERE job_key = ?", (job_key,)).fetchone()

    def record_failure(self, job_key: str, error: str, now: datetime) -> None:
        """수집 실패 upsert: failed, last_error, attempts += 1, ran_at = now. done 행은 바꾸지 않는다."""
        self._upsert_failed(job_key, error, None, now)

    def begin_sending(self, job_key: str, payload_json: str, now: datetime) -> None:
        """전송 전 선기록(R15 3단계): failed/'sending', payload_json, attempts += 1, ran_at = now.

        이미 저장된 payload_json은 바꾸지 않는다(재시도 간 내용·분할 고정).
        """
        self._upsert_failed(job_key, "sending", payload_json, now)

    def _upsert_failed(self, job_key: str, error: str, payload_json: str | None, now: datetime) -> None:
        with transaction(self._conn) as c:
            cur = c.execute(
                """
                INSERT INTO job_runs (job_key, status, attempts, ran_at, last_error, payload_json)
                VALUES (?, 'failed', 1, ?, ?, ?)
                ON CONFLICT (job_key) DO UPDATE
                   SET status = 'failed',
                       attempts = job_runs.attempts + 1,
                       ran_at = excluded.ran_at,
                       last_error = excluded.last_error,
                       payload_json = COALESCE(job_runs.payload_json, excluded.payload_json)
                 WHERE job_runs.status = 'failed'
                """,
                (job_key, utc_iso(now), error, payload_json),
            )
            if cur.rowcount == 0:
                raise RepositoryError(f"job {job_key}: 이미 done입니다")

    def mark_part_sent(self, job_key: str, part_index: int, message_id: str | None) -> None:
        """파트 `part_index` 전송 성공. `part_index == parts_sent`일 때만 갱신하고 0번 파트는 message_id가 필수다."""
        with transaction(self._conn) as c:
            row = c.execute("SELECT parts_sent FROM job_runs WHERE job_key = ?", (job_key,)).fetchone()
            if row is None:
                raise NotFoundError(f"job {job_key} 없음")
            if part_index != row["parts_sent"]:
                raise PartOrderError(
                    f"job {job_key}: 파트 {part_index} 기록 요청, 진행 카운터는 {row['parts_sent']}"
                )
            if part_index == 0 and not message_id:
                raise ValueError("0번 파트는 message_id가 필요합니다")
            c.execute(
                """
                UPDATE job_runs
                   SET parts_sent = parts_sent + 1, message_id = COALESCE(message_id, ?)
                 WHERE job_key = ?
                """,
                (message_id, job_key),
            )

    def mark_done(self, job_key: str, total_parts: int) -> None:
        """전 파트 성공 후에만 done을 기록한다."""
        with transaction(self._conn) as c:
            row = c.execute("SELECT parts_sent FROM job_runs WHERE job_key = ?", (job_key,)).fetchone()
            if row is None:
                raise NotFoundError(f"job {job_key} 없음")
            if row["parts_sent"] != total_parts:
                raise PartOrderError(f"job {job_key}: {total_parts}파트 중 {row['parts_sent']}개만 전송됨")
            c.execute("UPDATE job_runs SET status = 'done', last_error = NULL WHERE job_key = ?", (job_key,))

    def mark_send_failed(self, job_key: str, error: str) -> None:
        """전송 실패 코드를 기록한다(status는 선기록의 failed 그대로, attempts·ran_at 불변)."""
        with transaction(self._conn) as c:
            cur = c.execute(
                "UPDATE job_runs SET last_error = ? WHERE job_key = ? AND status = 'failed'", (error, job_key)
            )
            if cur.rowcount == 0:
                raise RepositoryError(f"job {job_key}: failed 행이 아닙니다")
