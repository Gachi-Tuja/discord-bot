"""S15 컴포지션 루트: 버전 가드, 설정 기반 결합, 길드 sync·로컬라이즈, 스케줄러 작업 순서, `run` 진입점."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord import app_commands

from ipobot import __main__ as cli
from ipobot.bot import app as app_module
from ipobot.bot.app import IpoBot, KoreanTranslator, Services, require_discord_version
from ipobot.bot.cogs.ipo import KOREAN_COMMAND_NAMES
from ipobot.config import load_settings
from ipobot.db.connection import open_database
from ipobot.services import alerts
from ipobot.services.judging import JudgingService
from ipobot.services.weekly import WeeklyService
from tests.fakes.discord_channel import FakeDiscord


def test_version_guard_uses_tuple_comparison(monkeypatch):
    require_discord_version((2, 5, 0))
    require_discord_version((2, 10, 0))  # 문자열 비교였다면 '2.10' < '2.5'
    with pytest.raises(RuntimeError, match="2.5"):
        require_discord_version((2, 4, 9))
    monkeypatch.setattr(discord, "version_info", (2, 4, 0, "final", 0))
    with pytest.raises(RuntimeError):
        require_discord_version()


def make_services(base_env, fake_clock) -> Services:
    settings = load_settings(base_env)
    return Services(
        settings,
        fake_clock,
        open_database(":memory:"),
        krx=SimpleNamespace(aclose=AsyncMock()),
        dart=SimpleNamespace(aclose=AsyncMock()),
        llm=SimpleNamespace(),
    )


async def test_services_wire_scheduler_jobs_in_order(base_env, fake_clock):
    svc = make_services(base_env, fake_clock)
    ipo_channel = FakeDiscord().channel()
    jobs = svc.scheduler_jobs(ipo_channel, FakeDiscord().channel())
    assert [name for name, _ in jobs] == ["weekly", "judging", "announcements"]
    weekly, judging, announcements = (job for _, job in jobs)
    assert isinstance(weekly.__self__, WeeklyService) and weekly.__self__.channel is ipo_channel
    assert isinstance(judging.__self__, JudgingService) and judging.__self__ is announcements.__self__
    assert judging.__self__.cfg.judge_max_attempts == svc.settings.judge_max_attempts
    krx, dart = svc.krx, svc.dart
    await svc.aclose()
    krx.aclose.assert_awaited_once()
    dart.aclose.assert_awaited_once()


async def test_services_default_adapters_from_settings(base_env, fake_clock):
    settings = load_settings(base_env)
    svc = Services(settings, fake_clock, open_database(":memory:"))
    try:
        assert type(svc.krx).__name__ == "HttpKrxClient"
        assert type(svc.dart).__name__ == "HttpDartClient"
        assert type(svc.llm).__name__ == "AnthropicLlmClient"
    finally:
        await svc.aclose()


async def test_bot_sets_default_alerts_and_syncs_guild_commands(base_env, fake_clock, monkeypatch):
    set_default = []
    monkeypatch.setattr(alerts, "set_default", set_default.append)
    svc = make_services(base_env, fake_clock)
    bot = IpoBot(svc.settings, fake_clock, svc)
    assert len(set_default) == 1 and isinstance(set_default[0], alerts.OperatorAlerts)
    assert bot.intents.message_content is False and bot.intents.members is False

    bot.tree.sync = AsyncMock(return_value=[])
    bot.scheduler_jobs = AsyncMock(return_value=[])
    bot.wait_until_ready = AsyncMock()
    try:
        await bot.setup_hook()
        guild = discord.Object(id=111)
        bot.tree.sync.assert_awaited_once()
        assert bot.tree.sync.await_args.kwargs["guild"].id == guild.id
        names = {c.name for c in bot.tree.get_commands(guild=guild)}
        assert names == {"upcoming_ipos", "predict"}
        assert bot.tree.get_commands() == []  # 전역 커맨드 없음(길드 sync만)
        assert isinstance(bot.tree.translator, KoreanTranslator)
    finally:
        await bot.close()


async def test_translator_localizes_upcoming_ipos_to_korean():
    translator = KoreanTranslator(KOREAN_COMMAND_NAMES)
    name_ctx = app_commands.TranslationContext(app_commands.TranslationContextLocation.command_name, None)
    desc_ctx = app_commands.TranslationContext(app_commands.TranslationContextLocation.command_description, None)
    s = app_commands.locale_str("upcoming_ipos")
    assert await translator.translate(s, discord.Locale.korean, name_ctx) == "상장예정"
    assert await translator.translate(s, discord.Locale.american_english, name_ctx) is None
    assert await translator.translate(s, discord.Locale.korean, desc_ctx) is None
    assert await translator.translate(app_commands.locale_str("predict"), discord.Locale.korean, name_ctx) is None


async def test_on_interaction_routes_to_expired_fallback(base_env, fake_clock):
    svc = make_services(base_env, fake_clock)
    bot = IpoBot(svc.settings, fake_clock, svc)
    send = AsyncMock()
    inter = SimpleNamespace(
        type=discord.InteractionType.component,
        data={"custom_id": "ipobot:confirm:gone"},
        response=SimpleNamespace(is_done=lambda: False, send_message=send),
    )
    try:
        await bot.on_interaction(inter)
        send.assert_awaited_once()
        assert send.await_args.kwargs == {"ephemeral": True}
    finally:
        await bot.close()


async def test_resolve_channel_uses_cache_then_fetch(base_env, fake_clock):
    svc = make_services(base_env, fake_clock)
    bot = IpoBot(svc.settings, fake_clock, svc)
    channel = FakeDiscord().channel()
    try:
        bot.get_channel = lambda _id: channel
        assert await bot.resolve_channel(222) is channel
        bot.get_channel = lambda _id: None
        bot.fetch_channel = AsyncMock(return_value=object())
        with pytest.raises(TypeError):
            await bot.resolve_channel(333)
    finally:
        await bot.close()


def test_cli_run_reports_config_error(monkeypatch, capsys):
    for name in ("DISCORD_TOKEN", "DISCORD_GUILD_ID"):
        monkeypatch.delenv(name, raising=False)
    assert cli.main(["run"]) == 2
    assert "설정 오류" in capsys.readouterr().err


def test_run_builds_bot_and_runs_with_token(monkeypatch, base_env):
    started = {}

    class FakeBot:
        def __init__(self, settings, clock):
            started["settings"] = settings

        def run(self, token, **kwargs):
            started["token"] = token

    monkeypatch.setattr(app_module, "IpoBot", FakeBot)
    assert app_module.run(base_env) == 0
    assert started["token"] == "test-token"
    assert started["settings"].discord_guild_id == 111
