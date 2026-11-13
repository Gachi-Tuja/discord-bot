"""디스코드 다중 파트 전송(R22, Part B deliver_parts 교체본).

- 파트별 nonce `{nonce_base}:{i}`. discord.py ≥2.5가 `enforce_nonce: true`를 붙여 창 안 중복을 막는다.
- 모든 `send`는 `wait_for(call_timeout)`로 상한을 둔다.
- 모호한 실패는 파트당 `budget` 안에서만 같은 nonce로 재시도하고, 넘으면 `send_ambiguous`.
- `NotFound`는 code 10008(nonce 원본 삭제)만 전달로 간주한다.
"""

from __future__ import annotations

import asyncio
import logging
import time
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
) -> Delivered | Failed:
    if not 0 <= start <= len(parts):
        raise ValueError(f"start {start}가 파트 범위 0..{len(parts)} 밖입니다")
    nonces = [part_nonce(nonce_base, i) for i in range(start, len(parts))]
    raise_alert = alert or alerts.operator_alert

    def record(i: int, message_id: str) -> None:
        if on_sent is not None:
            on_sent(i, message_id)

    for i, nonce in zip(range(start, len(parts)), nonces, strict=True):
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
