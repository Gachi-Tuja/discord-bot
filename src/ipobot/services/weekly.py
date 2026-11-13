"""주간 상장 예정 게시(S13, R15·R18·R21·R22).

- 슬롯: 설정 요일·시각(KST)의 가장 최근 예정 시각. 키는 그 날짜의 ISO 주(`weekly:2026-W05`), nonce는 `w2026-W05:{i}`.
- `weekly_due`: 예정 시각 ≤ now < 예정 시각 + grace 이고, 행이 없거나 `failed`이면서 `ran_at + 백오프` 경과.
  단 `last_error='sending'`(전송 중 크래시·진행 기록 실패)은 첫 전송 시도(ran_at)부터 `NONCE_RESEND_WINDOW`
  안에서는 백오프 없이 다음 틱에 재시도한다 — 같은 nonce가 디스코드 중복 제거 창(약 5분) 안에서 재전송돼야 이미
  게시된 파트가 중복되지 않는다. 창 안 재개는 ran_at을 유지하므로, 진행 기록이 계속 실패해도 창이 지나면 일반
  백오프로 떨어진다(중복 공개 게시는 백오프당 최대 1건).
- `run_weekly`: payload_json이 있으면 재사용(DART 재호출 없음) → 없으면 수집·렌더링(실패 시 failed/dart_unavailable,
  메시지 0건) → 선기록(failed/sending) → 파트별 전송·진행 기록 → 전 파트 성공 시 done.
- grace를 벗어난 failed는 건너뛰고 ERROR 로그를 한 번 남긴다(일부 파트만 게시됐을 수 있음).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol

import discord

from ipobot.bot.formatting import digest_parts, parts_from_json, parts_to_json
from ipobot.clock import KST, Clock
from ipobot.db.repositories import JOB_LAST_ERROR_SENDING, JobRunRepo
from ipobot.domain.models import IpoListing
from ipobot.services.delivery import NONCE_RESEND_WINDOW, Delivered, deliver_parts
from ipobot.services.ipo_digest import DartUnavailable, digest_items

log = logging.getLogger(__name__)

LAST_ERROR_DART = "dart_unavailable"
LAST_ERROR_SENDING = JOB_LAST_ERROR_SENDING


@dataclass(frozen=True)
class WeeklySchedule:
    weekday: int  # 0=월 … 6=일
    post_time: time
    grace: timedelta
    retry_backoff: timedelta

    def __post_init__(self) -> None:
        if not 0 <= self.weekday <= 6:
            raise ValueError(f"weekday {self.weekday}는 0..6이어야 합니다")
        if self.grace <= timedelta(0) or self.retry_backoff <= timedelta(0):
            raise ValueError("grace와 retry_backoff는 양수여야 합니다")

    @classmethod
    def from_settings(cls, settings: Any) -> WeeklySchedule:
        return cls(
            weekday=settings.weekly_post_weekday,
            post_time=settings.weekly_post_time,
            grace=timedelta(hours=settings.weekly_post_grace_hours),
            retry_backoff=timedelta(minutes=settings.weekly_retry_backoff_minutes),
        )

    def latest_slot(self, now: datetime) -> datetime:
        """now 이하인 가장 최근 예정 시각(KST)."""
        local = now.astimezone(KST)
        day = local.date() - timedelta(days=(local.weekday() - self.weekday) % 7)
        slot = datetime.combine(day, self.post_time, tzinfo=KST)
        if slot > local:
            slot -= timedelta(days=7)
        return slot


def week_key(post_date: date) -> str:
    year, week, _ = post_date.isocalendar()
    return f"{year}-W{week:02d}"


def job_key(post_date: date) -> str:
    return f"weekly:{week_key(post_date)}"


def _ran_at(row: Mapping[str, Any]) -> datetime:
    return datetime.fromisoformat(row["ran_at"])


def in_grace(now: datetime, slot: datetime, schedule: WeeklySchedule) -> bool:
    return slot <= now < slot + schedule.grace


def weekly_due(now: datetime, last_run: Mapping[str, Any] | None, schedule: WeeklySchedule) -> bool:
    slot = schedule.latest_slot(now)
    if not in_grace(now, slot, schedule):
        return False
    if last_run is None:
        return True
    if last_run["status"] != "failed":
        return False
    if resumes_in_nonce_window(now, last_run):
        return True
    return now >= _ran_at(last_run) + schedule.retry_backoff


def resumes_in_nonce_window(now: datetime, last_run: Mapping[str, Any] | None) -> bool:
    """'sending' 행을 첫 전송 시도 시각부터 nonce 재전송 창 안에서 다시 보내는 경우인가."""
    return (
        last_run is not None
        and last_run["status"] == "failed"
        and last_run["last_error"] == LAST_ERROR_SENDING
        and now < _ran_at(last_run) + NONCE_RESEND_WINDOW
    )


class DigestBuilder(Protocol):
    async def build_digest(self, post_date: date) -> list[IpoListing]: ...


class SummarySource(Protocol):
    async def summaries_for(self, listings: Sequence[IpoListing]) -> dict[str, str | None]: ...


@dataclass
class WeeklyService:
    conn: sqlite3.Connection
    digest: DigestBuilder
    channel: discord.abc.Messageable
    clock: Clock
    schedule: WeeklySchedule
    summaries: SummarySource | None = None
    # deliver_parts 추가 인자(테스트의 sleep·retry_delays 주입용)
    delivery_options: Mapping[str, Any] = field(default_factory=dict)
    _skip_logged: set[str] = field(default_factory=set, init=False, repr=False)

    def __post_init__(self) -> None:
        self.jobs = JobRunRepo(self.conn)

    async def run_weekly(self) -> None:
        now = self.clock.now()
        slot = self.schedule.latest_slot(now)
        post_date = slot.date()
        key = job_key(post_date)
        row = self.jobs.get(key)
        if not weekly_due(now, row, self.schedule):
            self._log_skipped_failure(key, row, now, slot)
            return
        if row is not None and row["payload_json"] is not None:
            parts = parts_from_json(row["payload_json"])
            start = row["parts_sent"]
        else:
            try:
                listings = await self.digest.build_digest(post_date)
            except DartUnavailable as exc:
                log.warning("weekly_dart_unavailable job=%s error=%s", key, exc)
                self.jobs.record_failure(key, LAST_ERROR_DART, now)
                return
            summaries = await self.summaries.summaries_for(listings) if self.summaries is not None else None
            parts = digest_parts(digest_items(listings, summaries), post_date)
            start = 0
        # 창 안 재개는 첫 시도 시각을 유지해야 진행 기록이 계속 실패할 때 창이 끝나고 백오프로 떨어진다.
        self.jobs.begin_sending(key, parts_to_json(parts), now, keep_ran_at=resumes_in_nonce_window(now, row))
        outcome = await deliver_parts(
            self.channel,
            parts,
            nonce_base=f"w{week_key(post_date)}",
            start=start,
            on_sent=lambda i, message_id: self.jobs.mark_part_sent(key, i, message_id),
            **self.delivery_options,
        )
        if isinstance(outcome, Delivered):
            self.jobs.mark_done(key, len(parts))
            log.info("weekly_posted job=%s parts=%d", key, len(parts))
        else:
            log.warning("weekly_send_failed job=%s code=%s", key, outcome.code)
            self.jobs.mark_send_failed(key, outcome.code)

    def _log_skipped_failure(
        self, key: str, row: Mapping[str, Any] | None, now: datetime, slot: datetime
    ) -> None:
        if row is None or row["status"] != "failed" or in_grace(now, slot, self.schedule):
            return
        if key in self._skip_logged:
            return
        self._skip_logged.add(key)
        log.error(
            "weekly_failed_outside_grace job=%s last_error=%s parts_sent=%s attempts=%s "
            "(일부 파트만 게시된 상태일 수 있음)",
            key,
            row["last_error"],
            row["parts_sent"],
            row["attempts"],
        )

