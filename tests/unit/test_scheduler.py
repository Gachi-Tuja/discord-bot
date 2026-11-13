"""S15 스케줄러(R17, R23): 작업별 격리, 루프 생존, before_loop의 wait_until_ready, 시작 직후 1회 tick."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from ipobot.bot.cogs.scheduler import SchedulerCog


class Bot:
    def __init__(self) -> None:
        self.ready = asyncio.Event()
        self.wait_until_ready = AsyncMock(side_effect=self.ready.wait)


def jobs_factory(*jobs):
    return AsyncMock(return_value=list(jobs))


async def test_weekly_failure_still_runs_judging_and_announcements():
    order: list[str] = []

    async def weekly():
        order.append("weekly")
        raise RuntimeError("boom")

    async def judging():
        order.append("judging")

    async def announcements():
        order.append("announcements")

    cog = SchedulerCog(Bot(), jobs_factory(("weekly", weekly), ("judging", judging), ("announcements", announcements)), 60)
    await cog.tick()
    assert order == ["weekly", "judging", "announcements"]


async def test_job_factory_failure_retried_next_tick():
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("channel not found")
        return [("judging", judging)]

    judging = AsyncMock()
    cog = SchedulerCog(Bot(), factory, 60)
    await cog.tick()
    judging.assert_not_awaited()
    await cog.tick()
    await cog.tick()
    assert calls == 2  # 성공한 구성은 재사용한다
    assert judging.await_count == 2


async def test_before_loop_waits_ready_then_ticks_immediately():
    bot = Bot()
    ticked = asyncio.Event()

    async def job():
        ticked.set()

    cog = SchedulerCog(bot, jobs_factory(("judging", job)), 3600)
    await cog.cog_load()
    try:
        await asyncio.sleep(0.05)
        bot.wait_until_ready.assert_awaited()
        assert not ticked.is_set()  # 준비 전에는 tick하지 않는다
        bot.ready.set()
        await asyncio.wait_for(ticked.wait(), 1)  # 간격(1시간)을 기다리지 않고 즉시 1회
    finally:
        await cog.cog_unload()
    await asyncio.sleep(0.01)
    assert not cog.loop.is_running()


async def test_exception_does_not_stop_loop():
    bot = Bot()
    bot.ready.set()
    calls = 0
    enough = asyncio.Event()

    async def failing():
        nonlocal calls
        calls += 1
        if calls >= 3:
            enough.set()
        raise RuntimeError("judging group failed")

    cog = SchedulerCog(bot, jobs_factory(("judging", failing)), 0.01)
    await cog.cog_load()
    try:
        await asyncio.wait_for(enough.wait(), 2)
        assert cog.loop.is_running()
        assert cog.loop.failed() is False
    finally:
        await cog.cog_unload()
