"""스케줄러 tick(R17, R23).

`before_loop`에서 `wait_until_ready`를 기다린 뒤 곧바로 1회 tick하고, 이후 `SCHEDULER_TICK_SECONDS`마다 돈다.
모든 상태는 DB에 있으므로 tick은 매번 DB를 보고 할 일을 정한다. tick은 주간 게시·판정·공지를 각각
try/except로 감싸고, 루프 자체는 예외로 멈추지 않는다.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from discord.ext import commands, tasks

log = logging.getLogger(__name__)

Job = tuple[str, Callable[[], Awaitable[None]]]
JobFactory = Callable[[], Awaitable[Sequence[Job]]]


class ReadyWaiter(Protocol):
    async def wait_until_ready(self) -> None: ...


class SchedulerCog(commands.Cog):
    def __init__(self, bot: ReadyWaiter, job_factory: JobFactory, tick_seconds: float) -> None:
        self.bot = bot
        self._job_factory = job_factory
        self._jobs: Sequence[Job] | None = None
        self.loop = tasks.loop(seconds=tick_seconds)(self.tick)
        self.loop.before_loop(self.before_loop)

    async def cog_load(self) -> None:
        self.loop.start()

    async def cog_unload(self) -> None:
        self.loop.cancel()

    async def before_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def tick(self) -> None:
        try:
            if self._jobs is None:
                # 채널 해석 등은 준비 후에만 가능하다. 실패하면 다음 tick에 다시 시도한다.
                self._jobs = list(await self._job_factory())
            jobs = self._jobs
        except Exception:
            log.exception("스케줄러 작업 구성 실패(다음 tick 재시도)")
            return
        for name, job in jobs:
            try:
                await job()
            except Exception:
                log.exception("스케줄러 작업 실패 job=%s", name)
