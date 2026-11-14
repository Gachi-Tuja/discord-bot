"""판정 실행과 결과 공지(S12, R9·R10·R11·R13·R19·R22·R23, Part A1).

`run_judging`
- 대상: `status='pending' AND next_attempt_at ≤ now`(그룹 = 판정 행 1개, R12).
- 관측: `first_open_on_or_after(target_date)`부터 오늘 미만의 개장일을 순서대로 조회한다. 확정 공개일은 DB 캐시다.
  첫 Traded에서 멈춘다(그 날 자체가 앞선 AllEmpty의 공개 증거라 뒤쪽 관측이 결정에 영향을 주지 않는다).
- `decide()`(순수 정책) 뒤 closed day 재조회와 `decide()` 1회 재실행은 이 서비스가 맡는다(R13 출력과 저장 1단계, ARCH-R4-04).
- 저장은 `JudgmentRepo.apply_decision` 트랜잭션 1개다(Gate3 F3). 서비스는 트랜잭션 사이에서만 await한다(R24).
- 그룹마다 격리한다(R23): 예외가 나면 last_error·next_attempt_at만 바꾸고 streak은 그대로 둔 채 다음 그룹으로 간다.

`run_announcements`
- 결과 공지(R22): 종결·미공지·`next_attempt_at ≤ now` 행. nonce `j{id}:{i}`, 파트마다 진행 카운터를 기록한다.
  실패하면 `announce_error`와 `next_attempt_at = now + 간격`(Gate3 F2). `on_sent` 기록 예외는 첫 전송 시도부터
  `NONCE_RESEND_WINDOW` 안에서만 백오프 없이 다음 주기에 같은 start로 재시도한다(Gate3 F7). 창이 지나면 첫 시도
  시각 + 간격까지 쉬므로, 진행 기록이 계속 실패해도 중복 공개 게시는 간격당 최대 1건이다(첫 시도 시각은 메모리에
  두며 재시작하면 초기화된다).
- 인증 오류 지연 안내(Part A1): 판정 시도 결과가 Wait이고 그 시도에서 auth를 관측했으며 아직 안내하지 않았을 때만
  보낸다(Gate3 F4). nonce `a{id}:{i}`. 실패 정책은 결과 공지와 같고, 실패해도 판정 전이를 막지 않는다.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol

import discord

from ipobot.bot.formatting import auth_delay_parts, result_parts
from ipobot.clock import KST, Clock, today_kst
from ipobot.db.repositories import CalendarRepo, JudgmentRepo, PredictionRepo
from ipobot.domain.calendar import TradingCalendar
from ipobot.domain.dates import first_open_on_or_after
from ipobot.domain.judging_policy import (
    Adopt,
    Decision,
    PolicyConfig,
    Unjudgeable,
    Wait,
    decide,
)
from ipobot.domain.models import (
    EMPTY_STREAK,
    MARKETS,
    DayObs,
    FetchError,
    Market,
    Traded,
    UnavailStreak,
)
from ipobot.domain.scoring import GroupEntry, InvalidActual, rank_group
from ipobot.services import alerts
from ipobot.services.delivery import NONCE_RESEND_WINDOW, Delivered, deliver_parts

log = logging.getLogger(__name__)

LAST_ERROR_JUDGING = "judging_error"  # 그룹 처리 중 예외(R23)
REASON_INVALID_ACTUAL = "invalid_actual"  # R10 방어 코드


class JudgingQuotePort(Protocol):
    async def day_obs(
        self, code: str, trade_date: date, hint: Market | None, bypass_negative_cache: bool = False
    ) -> DayObs: ...

    async def market_published(
        self, trade_date: date, market: Market, bypass_negative_cache: bool = False
    ) -> bool | FetchError: ...


class JudgingCalendar(TradingCalendar, Protocol):
    def reload(self) -> None: ...

    def add_closed_day(self, d: date) -> None: ...


def _d(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _streak(row: Mapping[str, Any]) -> UnavailStreak:
    return UnavailStreak(
        since=_d(row["unavailable_since"]),
        last_day=_d(row["unavailable_last_day"]),
        days=row["unavailable_days"],
    )


@dataclass
class JudgingService:
    conn: sqlite3.Connection
    quotes: JudgingQuotePort
    cal: JudgingCalendar
    channel: discord.abc.Messageable
    clock: Clock
    cfg: PolicyConfig = field(default_factory=PolicyConfig)
    # deliver_parts 추가 인자(테스트의 sleep·retry_delays 주입용)
    delivery_options: Mapping[str, Any] = field(default_factory=dict)
    alert: Callable[[str, str], bool] | None = None
    # 지연 안내 대기열: judgment_id → 다음 전송 가능 시각. run_judging이 F4 조건으로 채우고,
    # 시작 시와 매 공지 주기에 DB에서 되살린다(재시작으로 대기열을 잃어도 안내가 1회 나간다).
    _auth_notice_due: dict[int, datetime] = field(default_factory=dict, init=False, repr=False)
    # 전송 중 예외(진행 기록 실패 등)가 난 공지의 첫 전송 시도 시각: judgment_id → 시각. nonce 재전송 창 판정용.
    _announce_first_try: dict[int, datetime] = field(default_factory=dict, init=False, repr=False)
    _auth_first_try: dict[int, datetime] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.judgments = JudgmentRepo(self.conn)
        self.predictions = PredictionRepo(self.conn)
        self.calendar_repo = CalendarRepo(self.conn)
        self._restore_auth_notices(self.clock.now())

    def _restore_auth_notices(self, now: datetime) -> None:
        for jid in self.judgments.pending_auth_notice_ids():
            self._auth_notice_due.setdefault(jid, now)

    @property
    def _interval(self) -> timedelta:
        return timedelta(minutes=self.cfg.krx_poll_interval_minutes)

    def _alert(self, kind: str, detail: str) -> None:
        (self.alert or alerts.operator_alert)(kind, detail)

    # ------------------------------------------------------------ 판정

    async def run_judging(self) -> None:
        now = self.clock.now()
        today = today_kst(self.clock)
        self.cal.reload()  # override·krx_closed_days 변경을 매 시도에 반영(재계획)
        for row in self.judgments.due_pending(now):
            jid = row["id"]
            try:
                decision = await self._judge(row, today, now)
            except Exception:
                log.exception("판정 시도 실패 judgment=%s", jid)
                self.judgments.mark_attempt_failed(jid, LAST_ERROR_JUDGING, now + self._interval, now)
                continue
            if decision.auth_seen:
                self._alert("krx_auth", f"judgment={jid} KRX 인증 오류")
            if (
                isinstance(decision.outcome, Wait)
                and decision.auth_seen
                and row["auth_delay_notified_at"] is None
            ):
                self._auth_notice_due.setdefault(jid, now)
            else:
                self._auth_notice_due.pop(jid, None)

    async def _judge(self, row: sqlite3.Row, today: date, now: datetime) -> Decision:
        jid = row["id"]
        code = row["stock_code"]
        hint: Market = row["market"]
        target = date.fromisoformat(row["target_date"])
        prev_frontier = date.fromisoformat(row["judge_trade_date"])
        streak = _streak(row)

        observations = await self._observe(code, hint, target, today)

        def run(obs: Mapping[date, DayObs]) -> Decision:
            return decide(target, obs, streak, prev_frontier, today, now, self.cfg, self.cal)

        decision = await self._confirm_closed_days(code, hint, observations, run(observations), run)

        results = []
        if isinstance(decision.outcome, Adopt):
            quote = decision.outcome.quote
            entries = [
                GroupEntry(p["id"], p["pred_price"], p["pred_market_cap"])
                for p in self.predictions.group_predictions(jid)
            ]
            try:
                results = rank_group(entries, quote.close_price, quote.market_cap)
            except InvalidActual:
                log.error("실제값 이상 judgment=%s close=%s mcap=%s", jid, quote.close_price, quote.market_cap)
                decision = replace(
                    decision,
                    outcome=Unjudgeable(REASON_INVALID_ACTUAL),
                    last_error=REASON_INVALID_ACTUAL,
                    streak=EMPTY_STREAK,
                )
        self.judgments.apply_decision(jid, decision, results, now)
        for mc in decision.closed_days:
            self.cal.add_closed_day(mc.day)
        return decision

    async def _observe(self, code: str, hint: Market, target: date, today: date) -> dict[date, DayObs]:
        observations: dict[date, DayObs] = {}
        d = first_open_on_or_after(target, self.cal)
        while d < today:
            if self.cal.is_open(d):
                obs = await self.quotes.day_obs(code, d, hint)
                observations[d] = obs
                if isinstance(obs, Traded):
                    break
            d += timedelta(days=1)
        return observations

    async def _confirm_closed_days(
        self,
        code: str,
        hint: Market,
        observations: dict[date, DayObs],
        decision: Decision,
        run: Callable[[Mapping[date, DayObs]], Decision],
    ) -> Decision:
        """R13 1단계: 아직 기록되지 않은 closed day를 세 시장 캐시 우회로 재조회한다.

        - 세 시장 모두 여전히 비공개 → 기록 대상으로 확정(WARN).
        - 하나라도 공개 → 그 날 관측을 캐시 우회 day_obs로 바꾸고 decide()를 1회 재실행한다.
        - 재조회 FetchError → 기록하지 않고 그 날을 그 오류로 바꿔 재실행한다(Wait, 같은 streak 규칙).
        재실행 결과의 closed day는 첫 재조회에서 확정된 날짜만 기록한다(재재조회 없음).
        """
        if not decision.closed_days:
            return decision
        known = self.calendar_repo.closed_days()
        confirmed: set[date] = set()
        changed = False
        for mc in decision.closed_days:
            if mc.day in known:
                continue
            published = [
                await self.quotes.market_published(mc.day, m, bypass_negative_cache=True) for m in MARKETS
            ]
            error = next((p for p in published if isinstance(p, FetchError)), None)
            if error is not None:
                log.warning("closed day 재조회 실패 date=%s kind=%s", mc.day, error.kind)
                observations[mc.day] = error
                changed = True
                break  # 이 날이 프런티어가 되므로 뒤쪽 재조회는 결정에 영향이 없다
            if any(published):
                observations[mc.day] = await self.quotes.day_obs(code, mc.day, hint, bypass_negative_cache=True)
                changed = True
                continue
            log.warning("krx_closed_day_detected %s evidence=%s", mc.day, mc.evidence)
            confirmed.add(mc.day)
        if changed:
            decision = run(observations)
        return replace(decision, closed_days=tuple(mc for mc in decision.closed_days if mc.day in confirmed))

    # ------------------------------------------------------------ 공지

    async def run_announcements(self) -> None:
        now = self.clock.now()
        self._restore_auth_notices(now)  # 기존 항목의 백오프 시각은 그대로 둔다
        for row in self.judgments.due_announcements(now):
            jid = row["id"]
            if self._resend_held_until(self._announce_first_try, jid, now) is not None:
                continue
            self._begin_try(self._announce_first_try, jid, now)
            try:
                await self._announce(row, now)
            except Exception:
                # on_sent 기록 실패 포함(Gate3 F7): 창 안이면 다음 주기에 같은 start로, 창 밖이면 간격 뒤 재시도한다.
                log.exception("결과 공지 실패 judgment=%s", jid)
            else:
                self._announce_first_try.pop(jid, None)
        for jid, due in list(self._auth_notice_due.items()):
            if due > now:
                continue
            held = self._resend_held_until(self._auth_first_try, jid, now)
            if held is not None:
                self._auth_notice_due[jid] = held
                continue
            self._begin_try(self._auth_first_try, jid, now)
            try:
                await self._notify_auth_delay(jid, now)
            except Exception:
                log.exception("지연 안내 실패 judgment=%s", jid)
            else:
                self._auth_first_try.pop(jid, None)

    def _begin_try(self, first_try: dict[int, datetime], jid: int, now: datetime) -> None:
        """창 안 재개는 첫 시도 시각을 유지한다. 그래야 진행 기록이 계속 실패할 때 창이 끝나고 백오프로 떨어진다."""
        first = first_try.get(jid)
        if first is None or not first <= now < first + NONCE_RESEND_WINDOW:
            first_try[jid] = now

    def _resend_held_until(self, first_try: dict[int, datetime], jid: int, now: datetime) -> datetime | None:
        """직전 시도가 예외로 끝났고 nonce 재전송 창이 지났으면, 첫 시도 시각 + 간격까지 쉰다."""
        first = first_try.get(jid)
        if first is None or now < first + NONCE_RESEND_WINDOW:  # 시계가 뒤로 가면 _begin_try가 창을 새로 연다
            return None
        resume = first + self._interval
        return resume if now < resume else None

    @staticmethod
    def _dedupe_after(row: Mapping[str, Any]) -> datetime:
        """공지·안내는 목표일 이전에 게시될 수 없다. 첫 시도 시각은 메모리뿐이라 재시작에도 안전한 하한을 쓴다."""
        return datetime.combine(date.fromisoformat(row["target_date"]), time(0), KST)

    async def _announce(self, row: sqlite3.Row, now: datetime) -> None:
        jid = row["id"]
        parts = result_parts(row, self.judgments.results(jid), self.predictions.group_predictions(jid))
        outcome = await deliver_parts(
            self.channel,
            parts,
            nonce_base=f"j{jid}",
            start=row["announce_parts_sent"],
            on_sent=lambda i, message_id: self.judgments.mark_part_sent(jid, i, message_id, now),
            dedupe_after=self._dedupe_after(row),
            **self.delivery_options,
        )
        if isinstance(outcome, Delivered):
            self.judgments.mark_announced(jid, len(parts), now)
        else:
            log.warning("결과 공지 전송 실패 judgment=%s code=%s", jid, outcome.code)
            self.judgments.mark_announce_failed(jid, outcome.code, now + self._interval, now)

    async def _notify_auth_delay(self, jid: int, now: datetime) -> None:
        row = self.judgments.get(jid)
        if row["status"] != "pending" or row["auth_delay_notified_at"] is not None:
            self._auth_notice_due.pop(jid, None)
            return
        preds = self.predictions.group_predictions(jid)
        corp = preds[0]["corp_name"] if preds else row["stock_code"]
        parts = auth_delay_parts(corp)
        outcome = await deliver_parts(
            self.channel,
            parts,
            nonce_base=f"a{jid}",
            start=row["auth_delay_parts_sent"],
            on_sent=lambda i, _message_id: self.judgments.mark_auth_delay_part_sent(jid, i, now),
            dedupe_after=self._dedupe_after(row),
            **self.delivery_options,
        )
        if isinstance(outcome, Delivered):
            self.judgments.mark_auth_delay_notified(jid, len(parts), now)
            self._auth_notice_due.pop(jid, None)
        else:
            log.warning("지연 안내 전송 실패 judgment=%s code=%s", jid, outcome.code)
            self._auth_notice_due[jid] = now + self._interval
