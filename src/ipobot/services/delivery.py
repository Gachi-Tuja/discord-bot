"""디스코드 다중 파트 전송(R22, Part B deliver_parts 교체본).

- 파트별 nonce `{nonce_base}:{i}`. discord.py ≥2.5가 `enforce_nonce: true`를 붙여 창 안 중복을 막는다.
- 모든 `send`는 `wait_for(call_timeout)`로 상한을 둔다.
- 모호한 실패는 파트당 `budget` 안에서만 같은 nonce로 재시도하고, 넘으면 `send_ambiguous`.
- `NotFound`는 code 10008(nonce 원본 삭제)만 전달로 간주한다.
- `on_sent`(진행 기록 콜백) 예외는 `ProgressRecordError`로 감싸 전파한다. 그 파트는 이미 게시되었을 수 있으므로,
  호출자는 백오프 없이 다음 tick에 같은 `start`로 재시도할 수 있지만 `NONCE_RESEND_WINDOW` 안에서만이다.
  디스코드 nonce 중복 제거 창(`NONCE_DEDUPE_WINDOW`, 약 5분)을 벗어난 재전송은 새 공개 메시지가 된다.
- `dedupe_after`를 주면 `start` 파트를 보내기 전에 채널 최근 기록(`dedupe_after` 이후, 최신 `HISTORY_SCAN_LIMIT`개)에서
  봇이 쓴 같은 내용의 메시지를 찾는다. 있으면 전송 없이 그 메시지를 전달로 기록한다(nonce 창 밖 재전송 중복 방지).
  기록을 읽지 못하면 경고만 남기고 보낸다. `require_dedupe`면 보내지 않고 `history_unavailable`로 실패한다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import aiohttp
import discord

from ipobot.config import (
    NONCE_MAX_LENGTH,
    SEND_CALL_TIMEOUT,
    SEND_INWINDOW_BUDGET,
    SEND_INWINDOW_RETRY_DELAYS,
)
from ipobot.services import alerts

log = logging.getLogger(__name__)

UNKNOWN_MESSAGE = 10008
UNKNOWN_CHANNEL = 10003
DELETED_MESSAGE_ID = "deleted"

# 디스코드가 같은 nonce를 중복 제거하는 대략적인 창. 이 창 밖의 같은 nonce 전송은 새 메시지로 게시된다.
NONCE_DEDUPE_WINDOW = timedelta(minutes=5)
# 첫 전송 시각부터 같은 nonce 재전송을 "중복 없음"으로 믿는 구간. 틱 지연·시계 오차를 고려해 1분 여유를 둔다.
NONCE_RESEND_WINDOW = NONCE_DEDUPE_WINDOW - timedelta(minutes=1)
# 재전송 전 중복 확인에서 훑는 최근 메시지 수(디스코드 history 1회 요청 상한).
HISTORY_SCAN_LIMIT = 100


class ProgressRecordError(RuntimeError):
    """파트는 전달(또는 기존 게시 확인)되었지만 `on_sent` 진행 기록이 실패했다."""

    def __init__(self, index: int) -> None:
        super().__init__(f"파트 {index} 진행 기록 실패")
        self.index = index


class SendablePart(Protocol):
    @property
    def kwargs(self) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class Delivered:
    sent: int  # 이번 호출에서 전달(또는 10008 간주)된 파트 수


@dataclass(frozen=True)
class Failed:
    code: str
    alert: bool = False
    index: int | None = None


OnSent = Callable[[int, str], object]
AlertFn = Callable[[str, str], object]


def part_nonce(nonce_base: str, i: int) -> str:
    nonce = f"{nonce_base}:{i}"
    if len(nonce) > NONCE_MAX_LENGTH:
        raise ValueError(f"nonce가 {NONCE_MAX_LENGTH}자를 넘습니다: {nonce!r}")
    return nonce


async def deliver_parts(
    channel: discord.abc.Messageable,
    parts: Sequence[SendablePart],
    nonce_base: str,
    start: int = 0,
    on_sent: OnSent | None = None,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    call_timeout: float = SEND_CALL_TIMEOUT,
    budget: float = SEND_INWINDOW_BUDGET,
    retry_delays: Sequence[float] = SEND_INWINDOW_RETRY_DELAYS,
    alert: AlertFn | None = None,
    dedupe_after: datetime | None = None,
    require_dedupe: bool = False,
) -> Delivered | Failed:
    if not 0 <= start <= len(parts):
        raise ValueError(f"start {start}가 파트 범위 0..{len(parts)} 밖입니다")
    nonces = [part_nonce(nonce_base, i) for i in range(start, len(parts))]
    raise_alert = alert or alerts.operator_alert

    def record(i: int, message_id: str) -> None:
        if on_sent is None:
            return
        try:
            on_sent(i, message_id)
        except Exception as e:
            raise ProgressRecordError(i) from e

    first = start
    if dedupe_after is not None and start < len(parts):
        found = await _find_posted(channel, parts[start], dedupe_after, call_timeout, raise_alert)
        if found is _UNAVAILABLE:
            if require_dedupe:
                log.error("history_unavailable 기록 확인 없이는 재전송하지 않음 nonce=%s", nonces[0])
                return Failed("history_unavailable", index=start)
        elif found is not None:
            log.warning("이미 게시된 파트를 채널 기록에서 확인, 재전송 생략 nonce=%s message=%s", nonces[0], found)
            record(start, str(found))
            first = start + 1

    for i, nonce in zip(range(first, len(parts)), nonces[first - start :], strict=True):
        kwargs = parts[i].kwargs
        t0 = monotonic()
        attempt = 0
        while True:
            try:
                message = await asyncio.wait_for(channel.send(**kwargs, nonce=nonce), timeout=call_timeout)
            except (TimeoutError, aiohttp.ClientError, discord.DiscordServerError) as e:
                elapsed = monotonic() - t0
                if attempt >= len(retry_delays):
                    log.error("send_ambiguous nonce=%s attempts=%d error=%r", nonce, attempt + 1, e)
                    return Failed("send_ambiguous", index=i)
                delay = retry_delays[attempt]
                if elapsed + delay + call_timeout > budget:
                    log.error(
                        "send_ambiguous(예산 초과) nonce=%s attempts=%d elapsed=%.1f error=%r",
                        nonce, attempt + 1, elapsed, e,
                    )
                    return Failed("send_ambiguous", index=i)
                log.warning("모호한 전송 실패, %.0f초 뒤 같은 nonce로 재시도 nonce=%s error=%r", delay, nonce, e)
                attempt += 1
                await sleep(delay)
                continue
            except discord.Forbidden as e:
                detail = f"nonce={nonce} status={e.status} code={e.code}"
                log.error("send_forbidden %s", detail)
                raise_alert("discord_forbidden", detail)
                return Failed("send_forbidden", alert=True, index=i)
            except discord.NotFound as e:
                if e.code == UNKNOWN_MESSAGE:
                    log.warning(
                        "nonce 원본이 삭제됨(status=%s code=%s), 전달로 간주 nonce=%s", e.status, e.code, nonce
                    )
                    record(i, DELETED_MESSAGE_ID)
                    break
                detail = f"nonce={nonce} status={e.status} code={e.code}"
                if e.code == UNKNOWN_CHANNEL:
                    log.error("send_channel_not_found %s", detail)
                    raise_alert("discord_channel_not_found", detail)
                    return Failed("send_channel_not_found", alert=True, index=i)
                log.error("send_not_found %s", detail)
                raise_alert("discord_not_found", detail)
                return Failed("send_not_found", alert=True, index=i)
            except discord.HTTPException as e:
                log.error("send_http_%s nonce=%s code=%s", e.status, nonce, e.code)
                return Failed(f"send_http_{e.status}", index=i)
            record(i, str(message.id))
            break

    return Delivered(sent=len(parts) - start)


_UNAVAILABLE = object()


def _norm(text: str | None) -> str:
    return (text or "").strip()


def _embed_key(e: discord.Embed) -> tuple[Any, ...]:
    d = e.to_dict()
    fields = tuple((_norm(f.get("name")), _norm(f.get("value"))) for f in d.get("fields", ()))
    footer = _norm((d.get("footer") or {}).get("text"))
    return (_norm(d.get("title")), _norm(d.get("description")), fields, footer)


def fingerprint(content: str | None, embeds: Sequence[discord.Embed]) -> tuple[Any, ...]:
    """게시 내용 비교 키. 디스코드가 앞뒤 공백을 다듬을 수 있어 정규화한다."""
    return (_norm(content), tuple(_embed_key(e) for e in embeds))


async def _find_posted(
    channel: discord.abc.Messageable,
    part: SendablePart,
    after: datetime,
    call_timeout: float,
    raise_alert: AlertFn,
) -> object:
    """봇이 `after` 이후 게시한 같은 내용의 메시지 id. 없으면 None, 확인할 수 없으면 `_UNAVAILABLE`."""
    me = getattr(getattr(channel, "guild", None), "me", None)
    if me is None:
        log.warning("채널 기록 확인 불가: 길드 채널이 아님")
        return _UNAVAILABLE
    kwargs = part.kwargs
    target = fingerprint(kwargs.get("content"), kwargs.get("embeds") or ())

    async def scan() -> int | None:
        async for m in channel.history(limit=HISTORY_SCAN_LIMIT, after=after, oldest_first=False):
            if m.author.id == me.id and fingerprint(m.content, m.embeds) == target:
                return m.id
        return None

    try:
        return await asyncio.wait_for(scan(), timeout=call_timeout)
    except discord.Forbidden as e:
        detail = f"status={e.status} code={e.code}"
        log.error("history_forbidden %s", detail)
        raise_alert("discord_history_forbidden", detail)
    except (TimeoutError, aiohttp.ClientError, discord.HTTPException) as e:
        log.warning("채널 기록 확인 실패 error=%r", e)
    return _UNAVAILABLE

