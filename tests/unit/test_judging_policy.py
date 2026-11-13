"""S3b: 순수 판정 정책 decide() (R9 지연 상한, R13, Part A1 auth_seen, Part B 사례 18)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from ipobot.clock import KST
from ipobot.domain.judging_policy import (
    Adopt,
    MarketClosed,
    PolicyConfig,
    Unjudgeable,
    Wait,
    decide,
)
from ipobot.domain.models import (
    EMPTY_STREAK,
    AllEmpty,
    DailyQuote,
    FetchError,
    NotTraded,
    Traded,
    UnavailStreak,
    Unpublished,
)

CFG = PolicyConfig()  # 09:00, 15:00, 30분, 시도 5, stale 10
STALE = CFG.judge_stale_business_days


class WeekdayCalendar:
    def __init__(self, closed: set[date] | None = None) -> None:
        self.closed = closed or set()

    def is_open(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.closed


CAL = WeekdayCalendar()


def at(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=KST)


def quote(d: date, close: int = 10_000) -> DailyQuote:
    return DailyQuote(
        trade_date=d,
        market="KOSDAQ",
        stock_code="123456",
        corp_name="테스트",
        close_price=close,
        open_price=close,
        trade_volume=1000,
        market_cap=close * 1_000_000,
        listed_shares=1_000_000,
    )


def open_days(start: date, end: date) -> list[date]:
    """start 이상 end 미만의 개장일."""
    out = []
    d = start
    while d < end:
        if CAL.is_open(d):
            out.append(d)
        d += timedelta(days=1)
    return out


MON, TUE, WED, THU, FRI = (date(2026, 11, 2) + timedelta(days=i) for i in range(5))
SAT = date(2026, 11, 7)
NEXT_MON = date(2026, 11, 9)
NEXT_TUE = date(2026, 11, 10)
NEXT_WED = date(2026, 11, 11)


def run(obs, *, today, now, streak=EMPTY_STREAK, prev=None, target=None, cfg=CFG, cal=CAL):
    target = target or min(obs, default=today)
    prev = prev or min(obs, default=today)
    return decide(target, obs, streak, prev, today, now, cfg, cal)


# 1
def test_traded_adopts_and_clears_streak():
    streak = UnavailStreak(MON, MON, 1)
    r = run({MON: Traded(quote(MON))}, today=TUE, now=at(TUE, 9), streak=streak, prev=MON)
    assert r.outcome == Adopt(MON, quote(MON))
    assert r.judge_trade_date == MON
    assert r.streak == EMPTY_STREAK
    assert r.last_error is None
    assert r.next_attempt_at == at(TUE, 9)
    assert r.not_traded_days == 0


# 2
def test_all_empty_then_traded_is_market_closed_and_adopts_next():
    r = run({MON: AllEmpty(), TUE: Traded(quote(TUE))}, today=WED, now=at(WED, 9))
    assert r.outcome == Adopt(TUE, quote(TUE))
    assert r.closed_days == (MarketClosed(MON, TUE),)


# 3
def test_all_empty_without_later_published_waits_in_session_and_after():
    obs = {MON: AllEmpty(), TUE: AllEmpty()}
    r = run(obs, today=WED, now=at(WED, 9, 10))
    assert isinstance(r.outcome, Wait) and r.outcome.frontier == MON
    assert r.judge_trade_date == MON
    assert r.last_error == "krx_all_empty"
    assert r.closed_days == ()
    assert r.next_attempt_at == at(WED, 9, 40)

    after = run(obs, today=WED, now=at(WED, 15, 30))
    assert after.next_attempt_at == at(THU, 9)


def test_wait_before_judge_time_uses_judge_time():
    r = run({MON: FetchError("transient")}, today=TUE, now=at(TUE, 8, 0))
    assert r.next_attempt_at == at(TUE, 9)


# 4
def test_calendar_open_krx_empty_with_later_publish_is_skipped():
    obs = {MON: AllEmpty(), TUE: NotTraded("halted"), WED: Traded(quote(WED))}
    r = run(obs, today=THU, now=at(THU, 9))
    assert r.outcome == Adopt(WED, quote(WED))
    assert r.closed_days == (MarketClosed(MON, TUE),)
    assert r.not_traded_days == 1


def test_unpublished_counts_as_published_evidence():
    obs = {MON: AllEmpty(), TUE: Unpublished(frozenset({"KONEX"})), WED: Traded(quote(WED))}
    r = run(obs, today=THU, now=at(THU, 9))
    assert r.closed_days == (MarketClosed(MON, TUE),)


def test_fetch_error_is_not_evidence_for_closure():
    r = run({MON: AllEmpty(), TUE: FetchError("transient")}, today=WED, now=at(WED, 9))
    assert isinstance(r.outcome, Wait) and r.outcome.frontier == MON
    assert r.closed_days == ()


# 5
def test_halted_then_traded():
    r = run({MON: NotTraded("halted"), TUE: Traded(quote(TUE))}, today=WED, now=at(WED, 9))
    assert r.outcome == Adopt(TUE, quote(TUE))
    assert r.not_traded_days == 1


# 6
@pytest.mark.parametrize(("last", "reason"), [("halted", "halted_suspected"), ("absent", "delisted_suspected")])
def test_five_not_traded_is_unjudgeable_by_last_reason(last, reason):
    days = [MON, TUE, WED, THU, FRI]
    obs = {d: NotTraded("halted") for d in days[:4]}
    obs[FRI] = NotTraded(last)
    r = run(obs, today=NEXT_MON, now=at(NEXT_MON, 9), streak=UnavailStreak(MON, MON, 1), prev=MON)
    assert r.outcome == Unjudgeable(reason)
    assert r.last_error == reason
    assert r.not_traded_days == 5
    assert r.judge_trade_date == FRI
    assert r.streak == EMPTY_STREAK
    assert r.next_attempt_at == at(NEXT_MON, 9)


# 7
def test_catch_up_over_ten_past_days():
    days = open_days(date(2026, 10, 26), NEXT_MON)
    assert len(days) == 10
    obs = {d: NotTraded("halted") for d in days[:3]}
    obs.update({d: Traded(quote(d, close=100 + i)) for i, d in enumerate(days[3:])})
    r = run(obs, today=NEXT_MON, now=at(NEXT_MON, 9))
    assert r.outcome == Adopt(days[3], quote(days[3], close=100))
    assert r.not_traded_days == 3


# 8
def test_transient_in_session_waits_without_touching_counters():
    streak = UnavailStreak(TUE, TUE, 1)
    r = run({MON: NotTraded("halted"), TUE: FetchError("transient")}, today=WED, now=at(WED, 10),
            streak=streak, prev=TUE, target=MON)
    assert isinstance(r.outcome, Wait) and r.outcome.frontier == TUE
    assert r.last_error == "krx_fetch"
    assert r.not_traded_days == 1
    assert r.streak == streak
    assert r.next_attempt_at == at(WED, 10, 30)


# 9
def test_konex_only_unpublished_within_and_after_delay_limit():
    obs = {MON: Unpublished(frozenset({"KONEX"}))}
    # 상한 = next_open(MON) @ 15:00 = TUE 15:00
    before = run(obs, today=TUE, now=at(TUE, 14, 59))
    assert isinstance(before.outcome, Wait) and before.outcome.frontier == MON
    assert before.last_error == "krx_unpublished"
    assert before.not_traded_days == 0

    after = run(obs, today=TUE, now=at(TUE, 15, 0))
    assert isinstance(after.outcome, Wait) and after.outcome.frontier is None
    assert after.not_traded_days == 1
    assert after.last_error is None


# 10
def test_stale_guard_after_close_only():
    streak = UnavailStreak(date(2026, 10, 20), FRI, STALE - 1)
    obs = {MON: FetchError("transient")}
    r = run(obs, today=NEXT_MON, now=at(NEXT_MON, 15, 30), streak=streak, prev=MON)
    assert r.outcome == Unjudgeable("krx_unavailable")
    assert r.last_error == "krx_unavailable"
    assert r.streak == UnavailStreak(date(2026, 10, 20), NEXT_MON, STALE)
    assert r.judge_trade_date == MON

    before = run(obs, today=NEXT_MON, now=at(NEXT_MON, 14, 0), streak=streak, prev=MON)
    assert isinstance(before.outcome, Wait)
    assert before.streak == streak


# 11
def test_exhausted_list_waits_on_first_open_today_and_clears_streak():
    streak = UnavailStreak(MON, MON, 1)
    obs = {MON: AllEmpty(), TUE: NotTraded("halted")}
    r = run(obs, today=WED, now=at(WED, 9), streak=streak, prev=MON)
    assert r.outcome == Wait(None)
    assert r.judge_trade_date == WED
    assert r.last_error is None
    assert r.streak == EMPTY_STREAK
    assert r.closed_days == (MarketClosed(MON, TUE),)

    empty = run({}, today=SAT, now=at(SAT, 9), target=FRI, prev=FRI)
    assert empty.judge_trade_date == NEXT_MON
    assert empty.next_attempt_at == at(NEXT_MON, 9)


# 12
def test_market_closed_between_not_traded_is_not_counted():
    obs = {MON: NotTraded("halted"), TUE: AllEmpty(), WED: NotTraded("halted"), THU: Traded(quote(THU))}
    r = run(obs, today=FRI, now=at(FRI, 9))
    assert r.outcome == Adopt(THU, quote(THU))
    assert r.not_traded_days == 2
    assert r.closed_days == (MarketClosed(TUE, WED),)


# 13
def test_long_downtime_first_transient_after_close_starts_streak():
    start = date(2026, 10, 19)
    days = open_days(start, NEXT_MON)
    assert len(days) == 15
    obs = {days[0]: FetchError("transient")}
    r = run(obs, today=NEXT_MON, now=at(NEXT_MON, 15, 30), prev=days[0], target=start)
    assert isinstance(r.outcome, Wait)
    assert r.streak == UnavailStreak(NEXT_MON, NEXT_MON, 1)
    assert r.next_attempt_at == at(NEXT_TUE, 9)


# 14
def test_auth_never_counts_and_never_unjudgeable():
    streak = UnavailStreak(date(2026, 10, 20), FRI, STALE - 1)
    r = run({MON: FetchError("auth")}, today=NEXT_MON, now=at(NEXT_MON, 15, 30), streak=streak, prev=MON)
    assert isinstance(r.outcome, Wait)
    assert r.last_error == "krx_auth"
    assert r.streak == streak

    big = UnavailStreak(date(2026, 10, 1), FRI, STALE + 5)
    r2 = run({MON: FetchError("auth")}, today=NEXT_MON, now=at(NEXT_MON, 16), streak=big, prev=MON)
    assert isinstance(r2.outcome, Wait)
    assert r2.streak == big


# 15
def test_frontier_move_resets_streak_to_one():
    streak = UnavailStreak(TUE, WED, 2)
    obs = {MON: NotTraded("halted"), TUE: NotTraded("halted"), WED: FetchError("transient")}
    r = run(obs, today=THU, now=at(THU, 15, 30), streak=streak, prev=MON, target=MON)
    assert r.judge_trade_date == WED
    assert r.streak == UnavailStreak(THU, THU, 1)

    in_session = run(obs, today=THU, now=at(THU, 10), streak=streak, prev=MON, target=MON)
    assert in_session.streak == EMPTY_STREAK


# 16
def test_same_day_counted_once_and_weekend_not_counted():
    streak = UnavailStreak(WED, WED, 1)
    obs = {MON: FetchError("transient")}
    again = run(obs, today=WED, now=at(WED, 16), streak=streak, prev=MON)
    assert again.streak == streak

    weekend = run({MON: FetchError("transient")}, today=SAT, now=at(SAT, 16),
                  streak=UnavailStreak(FRI, FRI, 1), prev=MON)
    assert weekend.streak == UnavailStreak(FRI, FRI, 1)
    assert weekend.next_attempt_at == at(NEXT_MON, 9)


# 17
@pytest.mark.parametrize(("obs", "code"), [
    (FetchError("data_anomaly"), "krx_data_anomaly"),
    (FetchError("transient"), "krx_fetch"),
    (AllEmpty(), "krx_all_empty"),
])
def test_unavailable_kinds_are_counted(obs, code):
    r = run({MON: obs}, today=TUE, now=at(TUE, 15, 0), prev=MON)
    assert r.last_error == code
    assert r.streak == UnavailStreak(TUE, TUE, 1)


# 18 (Part B)
def test_rerun_with_replaced_observations():
    first = run({MON: AllEmpty(), TUE: AllEmpty(), WED: Traded(quote(WED))}, today=THU, now=at(THU, 9),
                prev=MON)
    assert first.outcome == Adopt(WED, quote(WED))
    assert first.closed_days == (MarketClosed(MON, WED), MarketClosed(TUE, WED))

    streak = UnavailStreak(MON, MON, 1)
    rerun = run({MON: Traded(quote(MON)), TUE: FetchError("transient"), WED: Traded(quote(WED))},
                today=THU, now=at(THU, 9), streak=streak, prev=MON)
    assert rerun.outcome == Adopt(MON, quote(MON))
    assert rerun.closed_days == ()
    assert rerun.streak == EMPTY_STREAK

    rerun2 = run({MON: FetchError("transient"), TUE: AllEmpty(), WED: Traded(quote(WED))},
                 today=THU, now=at(THU, 14), streak=streak, prev=MON)
    assert rerun2.outcome == Wait(MON)
    assert rerun2.streak == streak
    assert rerun2.closed_days == ()


# Part A1: auth_seen
def test_auth_seen_true_when_any_observation_is_auth_streak_unchanged():
    streak = UnavailStreak(MON, MON, 1)
    r = run({MON: FetchError("auth"), TUE: AllEmpty()}, today=WED, now=at(WED, 15, 30), streak=streak, prev=MON)
    assert r.auth_seen is True
    assert r.streak == streak


def test_auth_seen_false_without_auth_observation_streak_unchanged():
    streak = UnavailStreak(MON, MON, 1)
    r = run({MON: FetchError("transient")}, today=WED, now=at(WED, 10), streak=streak, prev=MON)
    assert r.auth_seen is False
    assert r.streak == streak


def test_observation_out_of_range_rejected():
    with pytest.raises(ValueError):
        run({WED: AllEmpty()}, today=WED, now=at(WED, 9))
