"""S3c: deliver_parts (R22, Part B ARCH-R4-01/02, CRIT-P4-F2, Part A2 ARCH-R5-01)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta

import aiohttp
import discord
import pytest

from ipobot.bot.formatting import Part
from ipobot.services.delivery import (
    Delivered,
    Failed,
    ProgressRecordError,
    RecordFailureGuard,
    deliver_parts,
    part_nonce,
)
from tests.fakes.discord_channel import FakeDiscord, Resp, forbidden, not_found, server_error


@dataclass(frozen=True)
class P:
    content: str

    @property
    def kwargs(self):
        return {"content": self.content}


class FakeTime:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


class Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.alerts: list[tuple[str, str]] = []

    def on_sent(self, i, mid):
        self.sent.append((i, mid))

    def alert(self, kind, detail):
        self.alerts.append((kind, detail))


async def deliver(fake, parts, base="j7", start=0, clock=None, rec=None, **kw):
    clock = clock or FakeTime()
    rec = rec or Recorder()
    result = await deliver_parts(
        fake.channel(), parts, base, start=start, on_sent=rec.on_sent,
        sleep=clock.sleep, monotonic=clock.monotonic, alert=rec.alert, **kw,
    )
    return result, rec, clock


PARTS = [P("a"), P("b"), P("c")]


async def test_all_parts_sent_with_per_part_nonce():
    fake = FakeDiscord()
    result, rec, _ = await deliver(fake, PARTS)
    assert result == Delivered(sent=3)
    assert [n for n, _ in fake.calls] == ["j7:0", "j7:1", "j7:2"]
    assert [k["content"] for _, k in fake.calls] == ["a", "b", "c"]
    assert "enforce_nonce" not in fake.calls[0][1]
    assert rec.sent == [(0, "1000"), (1, "1001"), (2, "1002")]


async def test_resume_from_start_index():
    fake = FakeDiscord()
    result, rec, _ = await deliver(fake, PARTS, start=2)
    assert result == Delivered(sent=1)
    assert [n for n, _ in fake.calls] == ["j7:2"]
    assert rec.sent == [(2, "1000")]


async def test_start_equal_to_len_sends_nothing():
    fake = FakeDiscord()
    result, _, _ = await deliver(fake, PARTS, start=3)
    assert result == Delivered(sent=0)
    assert fake.calls == []


async def test_nonce_too_long_raises_before_sending():
    fake = FakeDiscord()
    with pytest.raises(ValueError):
        await deliver(fake, PARTS, base="x" * 24)
    assert fake.calls == []
    assert part_nonce("j1234567890", 12) == "j1234567890:12"
    assert part_nonce("w2026-W05", 3) == "w2026-W05:3"


async def test_send_hang_times_out_and_retries_same_nonce():
    gate = asyncio.Event()

    async def hang():
        await gate.wait()

    fake = FakeDiscord(script=[hang])
    result, rec, clock = await deliver(fake, [P("a")], call_timeout=0.05)
    assert result == Delivered(sent=1)
    assert [n for n, _ in fake.calls] == ["j7:0", "j7:0"]
    assert clock.sleeps == [2]
    assert len(fake.posted) == 1
    assert rec.sent == [(0, "1000")]


async def test_ambiguous_success_already_posted_is_deduped_by_nonce():
    fake = FakeDiscord()

    async def posted_then_5xx():
        # 첫 시도는 실제로 게시되었지만 응답이 5xx로 돌아온 경우
        fake.preposted("j7:0", 555)
        return server_error()

    fake.script = [posted_then_5xx]
    result, rec, _ = await deliver(fake, [P("a")])
    assert result == Delivered(sent=1)
    assert len(fake.posted) == 1
    assert rec.sent == [(0, "555")]


async def test_retry_delays_then_send_ambiguous_within_budget():
    fake = FakeDiscord(script=[server_error() for _ in range(4)])
    result, rec, clock = await deliver(fake, PARTS)
    assert result == Failed("send_ambiguous", index=0)
    assert len(fake.calls) == 4
    assert clock.sleeps == [2, 5, 10]
    assert rec.sent == []


@pytest.mark.parametrize("exc", [TimeoutError(), aiohttp.ClientConnectionError("reset"),
                                 aiohttp.ServerTimeoutError("t")])
async def test_transport_errors_are_ambiguous_and_retried(exc):
    fake = FakeDiscord(script=[exc])
    result, _, clock = await deliver(fake, [P("a")])
    assert result == Delivered(sent=1)
    assert clock.sleeps == [2]


async def test_budget_exceeded_stops_before_fourth_attempt():
    """Part A2(ARCH-R5-01): 시도마다 20초(타임아웃) 전진, 지연 2·5·10초."""
    clock = FakeTime()
    starts: list[float] = []

    def timing_out():
        async def step():
            starts.append(clock.t)
            clock.t += 20
            return TimeoutError()
        return step

    fake = FakeDiscord(script=[timing_out() for _ in range(4)])
    result, rec, _ = await deliver(fake, [P("a")], clock=clock)
    assert result == Failed("send_ambiguous", index=0)
    assert len(fake.calls) == 3
    assert starts == [0, 22, 47]
    assert starts[-1] <= 70
    assert clock.sleeps == [2, 5]
    assert rec.sent == []


async def test_budget_exceeded_with_thirty_second_attempts():
    clock = FakeTime()

    def slow_5xx():
        async def step():
            clock.t += 30
            return server_error()
        return step

    fake = FakeDiscord(script=[slow_5xx() for _ in range(4)])
    result, _, _ = await deliver(fake, [P("a")], clock=clock)
    assert result == Failed("send_ambiguous", index=0)
    assert len(fake.calls) < 4


async def test_not_found_10008_counts_as_delivered_and_continues(caplog):
    fake = FakeDiscord(script=[None, not_found(10008)])
    with caplog.at_level(logging.WARNING):
        result, rec, _ = await deliver(fake, PARTS)
    assert result == Delivered(sent=3)
    assert rec.sent == [(0, "1000"), (1, "deleted"), (2, "1001")]
    assert any(
        r.levelno == logging.WARNING and "status=404 code=10008" in r.getMessage() for r in caplog.records
    )
    assert rec.alerts == []


async def test_not_found_code_0_fails_with_alert(caplog):
    fake = FakeDiscord(script=[not_found(None)])
    with caplog.at_level(logging.ERROR):
        result, rec, _ = await deliver(fake, PARTS)
    assert result == Failed("send_not_found", alert=True, index=0)
    assert rec.sent == []
    assert [k for k, _ in rec.alerts] == ["discord_not_found"]
    assert any(r.levelno == logging.ERROR and "code=0" in r.getMessage() for r in caplog.records)
    assert any("status=404" in r.getMessage() for r in caplog.records if r.levelno == logging.ERROR)
    assert "status=404" in rec.alerts[0][1]
    assert len(fake.calls) == 1


async def test_unknown_channel_10003():
    fake = FakeDiscord(script=[not_found(10003)])
    result, rec, _ = await deliver(fake, PARTS)
    assert result == Failed("send_channel_not_found", alert=True, index=0)
    assert rec.alerts == [("discord_channel_not_found", "nonce=j7:0 status=404 code=10003")]
    assert len(fake.calls) == 1


async def test_forbidden_alerts_without_retry():
    fake = FakeDiscord(script=[None, forbidden()])
    result, rec, _ = await deliver(fake, PARTS)
    assert result == Failed("send_forbidden", alert=True, index=1)
    assert rec.sent == [(0, "1000")]
    assert [k for k, _ in rec.alerts] == ["discord_forbidden"]
    assert len(fake.calls) == 2


async def test_other_4xx_fails_without_retry_or_alert():
    fake = FakeDiscord(script=[discord.HTTPException(Resp(400, "Bad Request"), {"code": 50035, "message": "x"})])
    result, rec, clock = await deliver(fake, PARTS)
    assert result == Failed("send_http_400", index=0)
    assert rec.alerts == []
    assert clock.sleeps == []
    assert len(fake.calls) == 1


async def test_same_nonce_after_window_posts_duplicate(fake_clock):
    """Gate3 F5: 디스코드 nonce 중복 제거는 몇 분짜리 창이다. 창이 지나면 같은 nonce도 새로 게시된다."""
    fake = FakeDiscord(clock=fake_clock)
    first, _, _ = await deliver(fake, [P("a")])
    fake_clock.set(fake_clock.now() + timedelta(minutes=4, seconds=59))
    await deliver(fake, [P("a")])
    assert first == Delivered(sent=1)
    assert len(fake.posted) == 1
    fake_clock.set(fake_clock.now() + timedelta(minutes=5))
    await deliver(fake, [P("a")])
    assert fake.nonces == ["j7:0", "j7:0", "j7:0"]
    assert len(fake.posted) == 2


async def test_autospec_rejects_enforce_nonce():
    """fake 채널은 실제 TextChannel.send 시그니처를 따른다(enforce_nonce는 페이로드 계층이 붙인다)."""
    ch = FakeDiscord().channel()
    with pytest.raises(TypeError):
        await ch.send(content="x", nonce="j1:0", enforce_nonce=True)
    msg = await ch.send(content="x", nonce="j1:0")
    assert msg.nonce == "j1:0"


async def test_on_sent_exception_propagates_and_rerun_same_start_does_not_duplicate():
    """Gate3 F7: on_sent 예외는 전파된다. 다음 tick에 같은 start로 재시도하면 nonce 창 안에서 중복이 없다."""
    fake = FakeDiscord()

    def on_sent(i, mid):
        if i == 1:
            raise RuntimeError("db locked")

    with pytest.raises(RuntimeError):
        await deliver_parts(fake.channel(), PARTS, "j7", on_sent=on_sent)
    assert fake.nonces == ["j7:0", "j7:1"]

    rec = Recorder()
    result = await deliver_parts(fake.channel(), PARTS, "j7", start=1, on_sent=rec.on_sent)
    assert result == Delivered(sent=2)
    assert rec.sent == [(1, "1001"), (2, "1002")]
    assert len(fake.posted) == 3
    assert [m.nonce for m in fake.posted] == ["j7:0", "j7:1", "j7:2"]


# ------------------------------------------------- 채널 기록 중복 확인(nonce 창 밖 재전송)


async def test_on_sent_exception_is_wrapped_as_progress_record_error():
    fake = FakeDiscord()

    def on_sent(i, mid):
        raise RuntimeError("db locked")

    with pytest.raises(ProgressRecordError) as info:
        await deliver_parts(fake.channel(), PARTS, "j7", on_sent=on_sent)
    assert info.value.index == 0
    assert isinstance(info.value.__cause__, RuntimeError)


async def test_dedupe_after_window_records_existing_post_without_resending(fake_clock):
    fake = FakeDiscord(clock=fake_clock)
    since = fake_clock.now()
    fake_clock.set(since + timedelta(seconds=1))
    await deliver(fake, [P("a")])  # 게시됐지만 진행 기록을 잃었다고 가정
    fake_clock.set(fake_clock.now() + timedelta(minutes=30))  # nonce 창 밖
    result, rec, _ = await deliver(fake, PARTS, dedupe_after=since)
    assert result == Delivered(sent=3)
    assert rec.sent == [(0, "1000"), (1, "1001"), (2, "1002")]
    assert fake.nonces == ["j7:0", "j7:1", "j7:2"]  # 0번은 다시 보내지 않았다
    assert [m.content for m in fake.posted] == ["a", "b", "c"]
    assert fake.history_calls[0]["after"] == since
    assert fake.history_calls[0]["oldest_first"] is False


async def test_dedupe_checks_only_start_part(fake_clock):
    fake = FakeDiscord(clock=fake_clock)
    since = fake_clock.now()
    fake_clock.set(since + timedelta(seconds=1))
    await deliver(fake, [P("a")])
    fake_clock.set(fake_clock.now() + timedelta(minutes=30))
    result, rec, _ = await deliver(fake, PARTS, start=1, dedupe_after=since)
    assert result == Delivered(sent=2)
    assert rec.sent == [(1, "1001"), (2, "1002")]
    assert len(fake.history_calls) == 1


async def test_dedupe_ignores_other_authors_and_older_messages(fake_clock):
    fake = FakeDiscord(clock=fake_clock)
    fake.preposted("old", 900).content = "a"  # since 이전의 봇 메시지
    fake_clock.set(fake_clock.now() + timedelta(seconds=1))
    since = fake_clock.now()
    fake_clock.set(since + timedelta(seconds=1))
    fake.post_other("a")  # 다른 사용자가 같은 내용을 씀
    result, rec, _ = await deliver(fake, [P("a")], dedupe_after=since)
    assert result == Delivered(sent=1)
    assert fake.nonces == ["j7:0"]
    assert rec.sent == [(0, str(fake.posted[-1].id))]


async def test_dedupe_matches_embed_parts(fake_clock):
    fake = FakeDiscord(clock=fake_clock)
    since = fake_clock.now()
    fake_clock.set(since + timedelta(seconds=1))
    part = Part(embeds=({"title": "📊 결과", "description": "본문", "fields": [{"name": "1위", "value": "x"}]},))
    await deliver(fake, [part])
    fake_clock.set(fake_clock.now() + timedelta(minutes=30))
    other = Part(embeds=({"title": "📊 결과", "description": "본문", "fields": [{"name": "1위", "value": "y"}]},))
    result, rec, _ = await deliver(fake, [part, other], dedupe_after=since)
    assert result == Delivered(sent=2)
    assert len(fake.calls) == 2  # 0번 1회(첫 게시) + 1번 1회
    assert rec.sent[0] == (0, "1000")


async def test_dedupe_history_forbidden_alerts_and_still_sends(fake_clock):
    fake = FakeDiscord(clock=fake_clock)
    fake.history_script = [forbidden()]
    result, rec, _ = await deliver(fake, [P("a")], dedupe_after=fake_clock.now())
    assert result == Delivered(sent=1)
    assert [k for k, _ in rec.alerts] == ["discord_history_forbidden"]
    assert fake.nonces == ["j7:0"]


async def test_dedupe_history_error_with_require_dedupe_does_not_send(fake_clock):
    fake = FakeDiscord(clock=fake_clock)
    fake.history_script = [server_error()]
    result, rec, _ = await deliver(fake, [P("a")], dedupe_after=fake_clock.now(), require_dedupe=True)
    assert result == Failed("history_unavailable", index=0)
    assert fake.calls == []
    assert rec.sent == []


async def test_dedupe_found_but_record_fails_raises_without_sending(fake_clock):
    fake = FakeDiscord(clock=fake_clock)
    since = fake_clock.now()
    fake_clock.set(since + timedelta(seconds=1))
    await deliver(fake, [P("a")])
    fake_clock.set(fake_clock.now() + timedelta(minutes=30))

    def on_sent(i, mid):
        raise RuntimeError("db locked")

    with pytest.raises(ProgressRecordError):
        await deliver_parts(fake.channel(), [P("a")], "j7", on_sent=on_sent, dedupe_after=since)
    assert len(fake.calls) == 1
    assert len(fake.posted) == 1


# ------------------------------------------------- 연속 진행 기록 실패 가드


async def test_record_guard_alerts_once_and_holds_until_success(fake_clock):
    fake = FakeDiscord(clock=fake_clock)
    guard = RecordFailureGuard(limit=2)
    alerts = []

    def notify(kind, detail):
        alerts.append(kind)
        return True

    def fail(i, mid):
        raise RuntimeError("db")

    kwargs = dict(channel=fake.channel(), parts=[P("a")], nonce_base="j7")
    for _ in range(3):
        with pytest.raises(ProgressRecordError):
            await guard.deliver("k", notify, on_sent=fail, dedupe_after=fake_clock.now() - timedelta(minutes=1), **kwargs)
    assert alerts == ["progress_record_failing"]
    assert guard.held("k")
    assert len(fake.calls) == 1  # 이후 재시도는 채널 기록에서 기존 게시를 찾았다

    # 보류 중 기록 조회가 실패하면 보내지 않는다.
    fake.history_script = [server_error()]
    result = await guard.deliver("k", notify, dedupe_after=fake_clock.now(), **kwargs)
    assert result == Failed("history_unavailable", index=0)
    assert len(fake.calls) == 1

    await guard.deliver("k", notify, on_sent=lambda i, mid: None, dedupe_after=fake_clock.now() - timedelta(minutes=1), **kwargs)
    assert not guard.held("k")
