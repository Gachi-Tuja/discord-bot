"""S3c: deliver_parts (R22, Part B ARCH-R4-01/02, CRIT-P4-F2, Part A2 ARCH-R5-01)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import create_autospec

import aiohttp
import discord
import pytest

from ipobot.clock import KST
from ipobot.services.alerts import OperatorAlerts
from ipobot.services.delivery import Delivered, Failed, deliver_parts, part_nonce


@dataclass(frozen=True)
class P:
    content: str

    @property
    def kwargs(self):
        return {"content": self.content}


class Resp:
    def __init__(self, status: int, reason: str) -> None:
        self.status = status
        self.reason = reason


def not_found(code: int | None) -> discord.NotFound:
    msg = {"code": code, "message": "Unknown"} if code is not None else "<html>404</html>"
    return discord.NotFound(Resp(404, "Not Found"), msg)


def server_error() -> discord.DiscordServerError:
    return discord.DiscordServerError(Resp(503, "Service Unavailable"), "busy")


class FakeTime:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


@dataclass
class FakeDiscord:
    """nonce 창 안 중복을 흉내 낸다: 같은 nonce면 기존 메시지를 돌려준다."""

    script: list = field(default_factory=list)  # 호출 순서대로: None(성공) | 예외 | callable
    calls: list[tuple[str, dict]] = field(default_factory=list)
    by_nonce: dict[str, SimpleNamespace] = field(default_factory=dict)
    posted: list[SimpleNamespace] = field(default_factory=list)

    def channel(self):
        ch = create_autospec(discord.TextChannel, instance=True)
        ch.send.side_effect = self.send
        return ch

    async def send(self, *, nonce: str, **kwargs):
        self.calls.append((nonce, kwargs))
        step = self.script.pop(0) if self.script else None
        if callable(step) and not isinstance(step, BaseException):
            step = await step()
        if isinstance(step, BaseException):
            raise step
        if nonce in self.by_nonce:
            return self.by_nonce[nonce]
        msg = SimpleNamespace(id=1000 + len(self.posted), nonce=nonce, **kwargs)
        self.by_nonce[nonce] = msg
        self.posted.append(msg)
        return msg


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
        fake.by_nonce["j7:0"] = SimpleNamespace(id=555, nonce="j7:0")
        fake.posted.append(fake.by_nonce["j7:0"])
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
    assert any(r.levelno == logging.WARNING and "10008" in r.getMessage() for r in caplog.records)
    assert rec.alerts == []


async def test_not_found_code_0_fails_with_alert(caplog):
    fake = FakeDiscord(script=[not_found(None)])
    with caplog.at_level(logging.ERROR):
        result, rec, _ = await deliver(fake, PARTS)
    assert result == Failed("send_not_found", alert=True, index=0)
    assert rec.sent == []
    assert [k for k, _ in rec.alerts] == ["discord_not_found"]
    assert any(r.levelno == logging.ERROR and "code=0" in r.getMessage() for r in caplog.records)
    assert len(fake.calls) == 1


async def test_unknown_channel_10003():
    fake = FakeDiscord(script=[not_found(10003)])
    result, rec, _ = await deliver(fake, PARTS)
    assert result == Failed("send_channel_not_found", alert=True, index=0)
    assert [k for k, _ in rec.alerts] == ["discord_channel_not_found"]
    assert len(fake.calls) == 1


async def test_forbidden_alerts_without_retry():
    fake = FakeDiscord(script=[None, discord.Forbidden(Resp(403, "Forbidden"), {"code": 50013, "message": "x"})])
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


def test_operator_alert_once_per_day_per_kind(caplog, fake_clock):
    clock = fake_clock
    alerts = OperatorAlerts(clock)
    with caplog.at_level(logging.ERROR):
        assert alerts.alert("krx_auth", "401") is True
        assert alerts.alert("krx_auth", "401") is False
        assert alerts.alert("discord_forbidden", "x") is True
        clock.set(datetime(2026, 11, 13, 9, 0, tzinfo=KST))
        assert alerts.alert("krx_auth", "401") is True
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert errors == ["OPERATOR_ALERT krx_auth 401", "OPERATOR_ALERT discord_forbidden x",
                      "OPERATOR_ALERT krx_auth 401"]
