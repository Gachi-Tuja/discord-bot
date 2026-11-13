"""디스코드 채널 fake(S3c, Gate3 F5).

`create_autospec(discord.TextChannel, instance=True)` 위에 `send`만 바꿔 끼운다. 그래서 실제 시그니처에 없는
인자(예: `enforce_nonce`)는 TypeError로 거부된다. discord.py는 nonce를 붙이면 `enforce_nonce: true`를 보내고,
디스코드는 같은 nonce를 몇 분 동안만 중복 제거한다. fake는 주입 시계 기준 `dedupe_window`(기본 5분) 안의 같은
nonce에 기존 메시지를 돌려주고, 창이 지나면 새 메시지를 게시한다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Protocol
from unittest.mock import create_autospec

import discord

DEFAULT_DEDUPE_WINDOW = timedelta(minutes=5)


class _Clock(Protocol):
    def now(self) -> datetime: ...


class _FixedClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now


class Resp:
    """discord.HTTPException 생성용 최소 응답 객체."""

    def __init__(self, status: int, reason: str) -> None:
        self.status = status
        self.reason = reason


def not_found(code: int | None) -> discord.NotFound:
    msg = {"code": code, "message": "Unknown"} if code is not None else "<html>404</html>"
    return discord.NotFound(Resp(404, "Not Found"), msg)


def server_error() -> discord.DiscordServerError:
    return discord.DiscordServerError(Resp(503, "Service Unavailable"), "busy")


def http_error(status: int = 400, code: int = 50035) -> discord.HTTPException:
    return discord.HTTPException(Resp(status, "Error"), {"code": code, "message": "x"})


def forbidden() -> discord.Forbidden:
    return discord.Forbidden(Resp(403, "Forbidden"), {"code": 50013, "message": "x"})


class FakeDiscord:
    """`script`는 호출 순서대로 소비한다: None(성공) | 예외 | 코루틴 함수(결과가 None·예외)."""

    def __init__(
        self,
        script: list[Any] | None = None,
        clock: _Clock | None = None,
        dedupe_window: timedelta = DEFAULT_DEDUPE_WINDOW,
    ) -> None:
        self.script: list[Any] = list(script or [])
        self.clock: _Clock = clock or _FixedClock()
        self.dedupe_window = dedupe_window
        self.calls: list[tuple[str | None, dict[str, Any]]] = []
        self.by_nonce: dict[str, tuple[datetime, SimpleNamespace]] = {}
        self.posted: list[SimpleNamespace] = []

    def channel(self) -> Any:
        ch = create_autospec(discord.TextChannel, instance=True)
        ch.send.side_effect = self.send
        return ch

    def preposted(self, nonce: str, message_id: int) -> SimpleNamespace:
        """이미 게시된(응답만 유실된) 메시지를 심는다."""
        msg = SimpleNamespace(id=message_id, nonce=nonce)
        self.by_nonce[nonce] = (self.clock.now(), msg)
        self.posted.append(msg)
        return msg

    @property
    def nonces(self) -> list[str | None]:
        return [n for n, _ in self.calls]

    async def send(self, content: str | None = None, *, nonce: str | None = None, **kwargs: Any) -> SimpleNamespace:
        if content is not None:
            kwargs["content"] = content
        self.calls.append((nonce, kwargs))
        step = self.script.pop(0) if self.script else None
        if callable(step) and not isinstance(step, BaseException):
            step = await step()
        if isinstance(step, BaseException):
            raise step
        now = self.clock.now()
        if nonce is not None and nonce in self.by_nonce:
            first_seen, msg = self.by_nonce[nonce]
            if now - first_seen < self.dedupe_window:
                return msg
        msg = SimpleNamespace(id=1000 + len(self.posted), nonce=nonce, **kwargs)
        if nonce is not None:
            self.by_nonce[nonce] = (now, msg)
        self.posted.append(msg)
        return msg
