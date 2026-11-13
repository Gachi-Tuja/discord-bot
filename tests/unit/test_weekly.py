"""S13 주간 게시: 실제 sqlite + fake 다이제스트 + fake 디스코드 채널."""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

import pytest

from ipobot.bot.formatting import NO_UPCOMING_IPOS, parts_from_json
from ipobot.clock import KST, utc_iso
from ipobot.db.connection import open_database
from ipobot.db.repositories import JOB_LAST_ERROR_SENDING, JobRunRepo, PartOrderError, RepositoryError
from ipobot.domain.models import IpoListing
from ipobot.services.ipo_digest import DartUnavailable
from ipobot.services.delivery import NONCE_RESEND_WINDOW
from ipobot.services.weekly import LAST_ERROR_SENDING, WeeklySchedule, WeeklyService, job_key, week_key, weekly_due
from tests.conftest import FakeClock
from tests.fakes.discord_channel import FakeDiscord, forbidden

FRI = date(2026, 1, 30)  # ISO 2026-W05
KEY = "weekly:2026-W05"
SCHEDULE = WeeklySchedule(4, time(18, 0), timedelta(hours=6), timedelta(minutes=15))


def at(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=KST)


def listing(i: int, d: date = date(2026, 2, 3)) -> IpoListing:
    return IpoListing(f"{i:08d}", f"회사{i:02d}", d, f"2026012{i:06d}", market="KOSDAQ")


class FakeDigest:
    def __init__(self, listings=(), errors=()):
        self.listings = list(listings)
        self.errors = list(errors)
        self.calls: list[date] = []

    async def build_digest(self, post_date):
        self.calls.append(post_date)
        if self.errors:
            raise self.errors.pop(0)
        return self.listings


class FakeSummaries:
    async def summaries_for(self, listings):
        return {it.source_rcept_no: f"{it.corp_name} 요약" for it in listings}


async def _no_sleep(_):
    return None


class Env:
    def __init__(self, tmp_path, digest=None, now=None):
        self.conn = open_database(tmp_path / "w.sqlite3")
        self.clock = FakeClock(now or at(FRI, 18))
        self.digest = digest or FakeDigest([listing(1)])
        self.fake = FakeDiscord(clock=self.clock)
        self.svc = WeeklyService(
            conn=self.conn,
            digest=self.digest,
            channel=self.fake.channel(),
            clock=self.clock,
            schedule=SCHEDULE,
            summaries=FakeSummaries(),
            delivery_options={"sleep": _no_sleep, "retry_delays": (), "alert": lambda kind, detail: True},
        )
        self.jobs = JobRunRepo(self.conn)

    async def run(self, now=None):
        if now is not None:
            self.clock.set(now)
        await self.svc.run_weekly()

    def row(self):
        return self.jobs.get(KEY)


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.conn.close()


# ---------------------------------------------------------------- 스케줄


def test_week_key_is_iso_week():
    assert week_key(FRI) == "2026-W05"
    assert job_key(date(2027, 1, 1)) == "weekly:2026-W53"  # ISO 연도 경계


@pytest.mark.parametrize(
    ("now", "due"),
    [
        (at(FRI, 17, 59), False),
        (at(FRI, 18, 0), True),
        (at(FRI, 23, 59), True),
        (at(FRI + timedelta(days=1), 0, 0), False),  # 18:00 + 6h 경계(제외)
        (at(FRI - timedelta(days=1), 18, 0), False),
    ],
)
def test_weekly_due_window(now, due):
    assert weekly_due(now, None, SCHEDULE) is due


def test_latest_slot_grace_across_week_boundary():
    sched = WeeklySchedule(6, time(22, 0), timedelta(hours=6), timedelta(minutes=15))  # 일요일 22시
    sunday = date(2026, 2, 1)
    slot = sched.latest_slot(at(sunday + timedelta(days=1), 1, 0))
    assert slot == at(sunday, 22) and week_key(slot.date()) == "2026-W05"
    assert weekly_due(at(sunday + timedelta(days=1), 1, 0), None, sched)


def test_weekly_due_failed_backoff_and_done():
    failed = {"status": "failed", "ran_at": utc_iso(at(FRI, 18)), "last_error": "send_forbidden"}
    assert not weekly_due(at(FRI, 18, 14), failed, SCHEDULE)
    assert weekly_due(at(FRI, 18, 15), failed, SCHEDULE)
    done = {"status": "done", "ran_at": utc_iso(at(FRI, 18)), "last_error": None}
    assert not weekly_due(at(FRI, 18, 15), done, SCHEDULE)


def test_weekly_due_sending_retries_without_backoff_inside_grace():
    sending = {"status": "failed", "ran_at": utc_iso(at(FRI, 18)), "last_error": "sending"}
    assert weekly_due(at(FRI, 18, 1), sending, SCHEDULE)
    assert not weekly_due(at(FRI + timedelta(days=1), 0, 0), sending, SCHEDULE)  # grace 밖은 재시도하지 않는다


def test_weekly_due_sending_bypass_bounded_by_nonce_window():
    # 첫 전송 시도부터 NONCE_RESEND_WINDOW(4분) 안에서만 백오프를 건너뛰고, 그 뒤는 일반 백오프를 따른다.
    sending = {"status": "failed", "ran_at": utc_iso(at(FRI, 18)), "last_error": LAST_ERROR_SENDING}
    assert weekly_due(at(FRI, 18, 3), sending, SCHEDULE)
    assert at(FRI, 18) + NONCE_RESEND_WINDOW == at(FRI, 18, 4)
    assert not weekly_due(at(FRI, 18, 4), sending, SCHEDULE)
    assert not weekly_due(at(FRI, 18, 14), sending, SCHEDULE)
    assert weekly_due(at(FRI, 18, 15), sending, SCHEDULE)


def test_sending_literal_is_shared_with_repository():
    assert LAST_ERROR_SENDING == JOB_LAST_ERROR_SENDING == "sending"


# ---------------------------------------------------------------- 실행


async def test_not_due_before_slot(env):
    await env.run(at(FRI, 17, 59))
    assert env.row() is None and env.fake.calls == [] and env.digest.calls == []


async def test_posts_once_and_done(env):
    await env.run()
    row = env.row()
    assert (row["status"], row["parts_sent"], row["attempts"], row["last_error"]) == ("done", 1, 1, None)
    assert row["message_id"] is not None
    assert env.fake.nonces == ["w2026-W05:0"]
    embed = env.fake.calls[0][1]["embeds"][0]
    assert "회사01 요약" in embed.fields[0].value
    await env.run(at(FRI, 18, 30))
    await env.run(at(FRI, 23))
    assert len(env.fake.calls) == 1 and env.digest.calls == [FRI]


async def test_restart_after_done_does_not_repost(tmp_path):
    e = Env(tmp_path)
    await e.run()
    e2 = Env(tmp_path, digest=e.digest, now=at(FRI, 19))
    await e2.run()
    assert e2.fake.calls == [] and e.digest.calls == [FRI]
    e.conn.close()
    e2.conn.close()


async def test_dart_failure_sends_nothing_then_retry_after_backoff(tmp_path):
    e = Env(tmp_path, digest=FakeDigest([listing(1)], errors=[DartUnavailable("5xx")]))
    await e.run()
    row = e.row()
    assert (row["status"], row["last_error"], row["attempts"], row["payload_json"]) == (
        "failed",
        "dart_unavailable",
        1,
        None,
    )
    assert e.fake.calls == []
    await e.run()  # 같은 tick 재실행 없음
    await e.run(at(FRI, 18, 14))  # 백오프 전
    assert len(e.digest.calls) == 1
    await e.run(at(FRI, 18, 15))
    row = e.row()
    assert (row["status"], row["attempts"]) == ("done", 2)
    assert e.fake.nonces == ["w2026-W05:0"]
    e.conn.close()


async def test_send_failure_then_resend_after_backoff(env):
    env.fake.script = [forbidden()]
    await env.run()
    row = env.row()
    assert (row["status"], row["last_error"], row["parts_sent"]) == ("failed", "send_forbidden", 0)
    assert row["payload_json"] is not None
    await env.run(at(FRI, 18, 15))
    row = env.row()
    assert row["status"] == "done" and row["message_id"] is not None
    assert env.fake.nonces == ["w2026-W05:0", "w2026-W05:0"]
    assert env.digest.calls == [FRI]  # payload 고정: DART 재호출 없음


async def test_more_than_25_fields_two_parts(tmp_path):
    e = Env(tmp_path, digest=FakeDigest([listing(i) for i in range(30)]))
    seen = {}

    async def before_second():
        seen["status"] = e.row()["status"]

    e.fake.script = [None, before_second]
    await e.run()
    assert e.fake.nonces == ["w2026-W05:0", "w2026-W05:1"]
    assert len(e.fake.posted) == 2
    assert seen["status"] == "failed"
    row = e.row()
    assert (row["status"], row["parts_sent"]) == ("done", 2)
    e.conn.close()


async def test_second_part_failure_resumes_without_dart(tmp_path):
    e = Env(tmp_path, digest=FakeDigest([listing(i) for i in range(30)]))
    e.fake.script = [None, forbidden()]
    await e.run()
    row = e.row()
    assert (row["status"], row["parts_sent"], row["last_error"]) == ("failed", 1, "send_forbidden")
    payload = row["payload_json"]
    assert len(parts_from_json(payload)) == 2
    e.digest.listings = [listing(99)]  # 재시도에서 DART를 부르면 내용이 달라진다
    await e.run(at(FRI, 18, 15))
    row = e.row()
    assert (row["status"], row["parts_sent"], row["attempts"]) == ("done", 2, 2)
    assert row["payload_json"] == payload
    assert e.digest.calls == [FRI]
    assert e.fake.nonces == ["w2026-W05:0", "w2026-W05:1", "w2026-W05:1"]
    assert len(e.fake.posted) == 2  # 메시지 총 2개
    e.conn.close()


async def test_crash_after_first_part_recorded_resumes_from_second(tmp_path, monkeypatch):
    e = Env(tmp_path, digest=FakeDigest([listing(i) for i in range(30)]))
    real = e.svc.jobs.mark_part_sent

    def crash_after_first(key, i, message_id):
        real(key, i, message_id)
        if i == 0:
            raise RuntimeError("crash")

    monkeypatch.setattr(e.svc.jobs, "mark_part_sent", crash_after_first)
    with pytest.raises(RuntimeError):
        await e.run()
    row = e.row()
    assert (row["status"], row["last_error"], row["parts_sent"]) == ("failed", "sending", 1)
    monkeypatch.setattr(e.svc.jobs, "mark_part_sent", real)
    await e.run(at(FRI, 18, 1))  # 'sending'은 백오프 없이 다음 틱에 이어 보낸다
    assert e.row()["status"] == "done"
    assert e.fake.nonces == ["w2026-W05:0", "w2026-W05:1"]
    assert e.digest.calls == [FRI]
    e.conn.close()


async def test_crash_before_first_part_recorded_retries_next_tick_without_duplicate(tmp_path, monkeypatch):
    # 파트 0 게시 후 진행 기록 전에 죽으면 parts_sent=0, 'sending'으로 남는다. 다음 틱(+1분)이 같은 nonce로
    # 재전송해야 디스코드 nonce 중복 제거 창(5분) 안에 들어가 메시지가 하나로 유지된다.
    e = Env(tmp_path)
    real = e.svc.jobs.mark_part_sent

    def crash(key, i, message_id):
        raise RuntimeError("crash")

    monkeypatch.setattr(e.svc.jobs, "mark_part_sent", crash)
    with pytest.raises(RuntimeError):
        await e.run()
    assert (e.row()["last_error"], e.row()["parts_sent"]) == ("sending", 0)
    monkeypatch.setattr(e.svc.jobs, "mark_part_sent", real)
    e.clock.set(at(FRI, 18, 1))  # fake 디스코드의 nonce 창 시계도 +1분
    restarted = Env(tmp_path, digest=e.digest, now=at(FRI, 18, 1))
    restarted.fake = e.fake
    restarted.svc.channel = e.fake.channel()
    await restarted.run()
    row = restarted.row()
    assert (row["status"], row["parts_sent"]) == ("done", 1)
    assert e.fake.nonces == ["w2026-W05:0", "w2026-W05:0"]
    assert len(e.fake.posted) == 1
    assert e.digest.calls == [FRI]
    e.conn.close()
    restarted.conn.close()


async def test_persistent_progress_record_failure_is_throttled_by_backoff(tmp_path, monkeypatch):
    # 진행 기록이 grace 내내 실패해도 매 틱 재전송하지 않는다: nonce 창(첫 시도 기준 4분) 안 재시도 뒤 백오프.
    # 디스코드 중복 제거 창 밖 재전송만 새 공개 게시가 되므로 공개 게시는 백오프당 최대 1건이다.
    e = Env(tmp_path)

    def fail(key, i, message_id):
        raise RuntimeError("record failed")

    monkeypatch.setattr(e.svc.jobs, "mark_part_sent", fail)
    minutes = int(SCHEDULE.grace / timedelta(minutes=1))
    failures = 0
    for m in range(minutes):  # 60초 틱
        try:
            await e.run(at(FRI, 18) + timedelta(minutes=m))
        except RuntimeError:
            failures += 1
    assert failures == len(e.fake.calls)  # 전송마다 진행 기록 실패
    bound = SCHEDULE.grace // SCHEDULE.retry_backoff + 2
    assert len(e.fake.posted) <= bound, (len(e.fake.posted), len(e.fake.calls))
    # 백오프 주기마다 첫 시도 + 창 안(4분) 재시도 3회 = 최대 4회 전송
    assert len(e.fake.calls) <= 4 * bound
    assert e.row()["last_error"] == "sending"
    e.conn.close()


async def test_failed_outside_grace_is_skipped_with_error_log(env, caplog):
    env.fake.script = [forbidden()]
    await env.run(at(FRI, 23, 50))
    with caplog.at_level(logging.ERROR):
        await env.run(at(FRI + timedelta(days=1), 0, 5))
        await env.run(at(FRI + timedelta(days=1), 1, 0))
    assert len(env.fake.calls) == 1
    assert env.row()["status"] == "failed"
    assert caplog.text.count("weekly_failed_outside_grace") == 1
    assert "일부 파트만 게시된 상태일 수 있음" in caplog.text


async def test_zero_listings_posts_notice_once(tmp_path):
    e = Env(tmp_path, digest=FakeDigest([]))
    await e.run()
    assert e.fake.nonces == ["w2026-W05:0"]
    assert e.fake.calls[0][1]["content"] == NO_UPCOMING_IPOS
    assert e.row()["status"] == "done"
    e.conn.close()


# ---------------------------------------------------------------- JobRunRepo


def test_job_run_repo_guards(tmp_path):
    conn = open_database(tmp_path / "r.sqlite3")
    repo = JobRunRepo(conn)
    now = at(FRI, 18)
    repo.record_failure(KEY, "dart_unavailable", now)
    repo.begin_sending(KEY, "[1]", now + timedelta(minutes=15))
    repo.begin_sending(KEY, "[2]", now + timedelta(minutes=30))
    row = repo.get(KEY)
    assert (row["attempts"], row["payload_json"], row["last_error"]) == (3, "[1]", "sending")
    with pytest.raises(ValueError):
        repo.mark_part_sent(KEY, 0, None)
    with pytest.raises(PartOrderError):
        repo.mark_part_sent(KEY, 1, "m")
    repo.mark_part_sent(KEY, 0, "m0")
    with pytest.raises(PartOrderError):
        repo.mark_done(KEY, 2)
    repo.mark_done(KEY, 1)
    with pytest.raises(RepositoryError):
        repo.record_failure(KEY, "dart_unavailable", now)
    with pytest.raises(RepositoryError):
        repo.mark_send_failed(KEY, "send_forbidden")
    assert repo.get(KEY)["status"] == "done"
    conn.close()
