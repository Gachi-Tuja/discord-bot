"""순수 판정 정책(R9 지연 상한, R13 순회·streak·stale 가드). I/O가 없고 모든 입력을 주입받는다.

closed day 재조회·`decide()` 1회 재실행·저장은 서비스 계층(S12)이 맡는다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta

from ipobot.clock import KST
from ipobot.domain.calendar import TradingCalendar
from ipobot.domain.dates import attempt_morning, first_open_on_or_after, next_open
from ipobot.domain.models import (
    EMPTY_STREAK,
    AllEmpty,
    DailyQuote,
    DayObs,
    FetchError,
    NotTraded,
    NotTradedReason,
    Traded,
    UnavailStreak,
    Unpublished,
)

# 판정 행의 관측 코드(judgments.last_error, pending 상태)
LAST_ERROR_BY_FETCH_KIND = {
    "transient": "krx_fetch",
    "data_anomaly": "krx_data_anomaly",
    "auth": "krx_auth",
}
LAST_ERROR_ALL_EMPTY = "krx_all_empty"
LAST_ERROR_UNPUBLISHED = "krx_unpublished"

REASON_BY_NOT_TRADED: dict[NotTradedReason, str] = {
    "halted": "halted_suspected",
    "absent": "delisted_suspected",
}
REASON_KRX_UNAVAILABLE = "krx_unavailable"


@dataclass(frozen=True)
class PolicyConfig:
    judge_time: time = time(9, 0)
    krx_poll_deadline: time = time(15, 0)
    krx_poll_interval_minutes: int = 30
    judge_max_attempts: int = 5
    judge_stale_business_days: int = 10

    @classmethod
    def from_settings(cls, settings) -> PolicyConfig:
        return cls(
            judge_time=settings.judge_time,
            krx_poll_deadline=settings.krx_poll_deadline,
            krx_poll_interval_minutes=settings.krx_poll_interval_minutes,
            judge_max_attempts=settings.judge_max_attempts,
            judge_stale_business_days=settings.judge_stale_business_days,
        )


@dataclass(frozen=True)
class MarketClosed:
    """KRX 관측상 휴장으로 보이는 날(세 시장 빈 응답 + 이후 날짜 공개)."""

    day: date
    evidence: date


@dataclass(frozen=True)
class Adopt:
    day: date
    quote: DailyQuote


@dataclass(frozen=True)
class Wait:
    """pending 유지. frontier는 미가용 관측 날짜이며, 목록 소진이면 None이다."""

    frontier: date | None


@dataclass(frozen=True)
class Unjudgeable:
    reason: str


Outcome = Adopt | Wait | Unjudgeable


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    judge_trade_date: date
    not_traded_days: int
    closed_days: tuple[MarketClosed, ...]
    streak: UnavailStreak
    last_error: str | None
    next_attempt_at: datetime
    # 이번 관측 중 하나라도 FetchError('auth')이면 True(Part A1). streak에는 영향이 없다.
    auth_seen: bool


def _at(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=KST)


def _is_published(obs: DayObs) -> bool:
    """어느 시장이든 공개된 관측인가(AllEmpty 휴장 판정의 근거)."""
    return isinstance(obs, (Traded, NotTraded, Unpublished))


def _is_unavailable(obs: DayObs) -> bool:
    """미가용 프런티어(§8 용어). auth는 설정 오류라 제외한다."""
    if isinstance(obs, FetchError):
        return obs.kind in ("transient", "data_anomaly")
    return isinstance(obs, (AllEmpty, Unpublished))


def _frontier_error(obs: DayObs) -> str:
    if isinstance(obs, FetchError):
        return LAST_ERROR_BY_FETCH_KIND[obs.kind]
    if isinstance(obs, AllEmpty):
        return LAST_ERROR_ALL_EMPTY
    if isinstance(obs, Unpublished):
        return LAST_ERROR_UNPUBLISHED
    raise ValueError(f"프런티어가 될 수 없는 관측: {obs!r}")


def decide(
    target_date: date,
    observations: Mapping[date, DayObs],
    streak: UnavailStreak,
    prev_frontier: date,
    today: date,
    now: datetime,
    cfg: PolicyConfig,
    cal: TradingCalendar,
) -> Decision:
    """R13. observations는 first_open_on_or_after(target_date) 이상 today 미만 개장일의 관측이다."""
    start = first_open_on_or_after(target_date, cal)
    items = sorted(observations.items())
    for d, _ in items:
        if d < start or d >= today:
            raise ValueError(f"관측 날짜 {d}가 범위 [{start}, {today}) 밖입니다")

    auth_seen = any(isinstance(o, FetchError) and o.kind == "auth" for _, o in items)
    after_close = now >= _at(today, cfg.krx_poll_deadline)

    not_traded = 0
    closed: list[MarketClosed] = []
    frontier: tuple[date, DayObs] | None = None

    def terminal(outcome: Outcome, day: date, new_streak: UnavailStreak, last_error: str | None) -> Decision:
        return Decision(
            outcome=outcome,
            judge_trade_date=day,
            not_traded_days=not_traded,
            closed_days=tuple(closed),
            streak=new_streak,
            last_error=last_error,
            next_attempt_at=now,
            auth_seen=auth_seen,
        )

    for i, (d, obs) in enumerate(items):
        if isinstance(obs, Traded):
            return terminal(Adopt(d, obs.quote), d, EMPTY_STREAK, None)

        if isinstance(obs, Unpublished) and now >= _at(next_open(d, cal), cfg.krx_poll_deadline):
            obs = NotTraded("absent")  # R9 지연 상한 초과: 시도를 1회 소모한다

        if isinstance(obs, NotTraded):
            not_traded += 1
            if not_traded >= cfg.judge_max_attempts:
                reason = REASON_BY_NOT_TRADED[obs.reason]
                return terminal(Unjudgeable(reason), d, EMPTY_STREAK, reason)
            continue

        if isinstance(obs, AllEmpty):
            evidence = next((d2 for d2, o2 in items[i + 1 :] if _is_published(o2)), None)
            if evidence is not None:
                closed.append(MarketClosed(d, evidence))
                continue

        frontier = (d, obs)
        break

    frontier_day = frontier[0] if frontier else first_open_on_or_after(today, cal)

    new_streak = streak if frontier_day == prev_frontier else EMPTY_STREAK
    if frontier is None:
        new_streak = EMPTY_STREAK
    elif _is_unavailable(frontier[1]) and cal.is_open(today) and after_close and new_streak.last_day != today:
        new_streak = replace(new_streak, since=new_streak.since or today, last_day=today, days=new_streak.days + 1)
    # auth 프런티어는 프런티어 이동 리셋만 적용하고 그대로 둔다.

    if (
        frontier is not None
        and _is_unavailable(frontier[1])
        and after_close
        and new_streak.days >= cfg.judge_stale_business_days
    ):
        return terminal(Unjudgeable(REASON_KRX_UNAVAILABLE), frontier_day, new_streak, REASON_KRX_UNAVAILABLE)

    if cal.is_open(today) and not after_close:
        next_attempt_at = max(now + timedelta(minutes=cfg.krx_poll_interval_minutes), _at(today, cfg.judge_time))
    else:
        next_attempt_at = attempt_morning(next_open(today, cal), cfg.judge_time)

    return Decision(
        outcome=Wait(frontier[0] if frontier else None),
        judge_trade_date=frontier_day,
        not_traded_days=not_traded,
        closed_days=tuple(closed),
        streak=new_streak,
        last_error=_frontier_error(frontier[1]) if frontier else None,
        next_attempt_at=next_attempt_at,
        auth_seen=auth_seen,
    )
