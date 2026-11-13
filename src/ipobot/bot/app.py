"""컴포지션 루트: 설정 → DB·어댑터·서비스·cog 결합, 길드 커맨드 sync, `on_interaction` 만료 폴백.

- 채널 전송은 서비스의 `services/delivery.deliver_parts`만 거친다. 이 모듈은 채널에 직접 보내지 않는다.
- SQLite 연결은 1개이며 이벤트 루프 스레드에서만 쓴다(§5).
- 채널 객체는 준비(ready) 후에만 해석할 수 있으므로 주간·판정 서비스는 스케줄러 첫 tick에 만든다.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from ipobot.adapters.dart import HttpDartClient
from ipobot.adapters.krx import HttpKrxClient
from ipobot.adapters.llm import AnthropicLlmClient
from ipobot.bot.cogs.ipo import KOREAN_COMMAND_NAMES, IpoCog
from ipobot.bot.cogs.predict import PredictCog
from ipobot.bot.cogs.scheduler import Job, SchedulerCog
from ipobot.bot.errors import report_interaction_error
from ipobot.bot.views import DraftStore, expired_fallback
from ipobot.clock import Clock, SystemClock
from ipobot.config import Settings, load_settings
from ipobot.db.connection import open_database
from ipobot.db.repositories import CalendarRepo
from ipobot.domain.calendar import KrxCalendar
from ipobot.domain.judging_policy import PolicyConfig
from ipobot.services import alerts
from ipobot.services.alerts import OperatorAlerts
from ipobot.services.ipo_digest import IpoDigestService
from ipobot.services.judging import JudgingService
from ipobot.services.prediction_parser import PredictionParser
from ipobot.services.predictions import PredictionService
from ipobot.services.quotes import QuoteService
from ipobot.services.summaries import SummaryService
from ipobot.services.weekly import WeeklySchedule, WeeklyService

log = logging.getLogger(__name__)

MIN_DISCORD_VERSION = (2, 5)


def require_discord_version(info: Sequence[int] | None = None) -> None:
    """discord.py ≥2.5(nonce → enforce_nonce 자동 부착)를 요구한다. 튜플 비교(ARCH-R4-03)."""
    version = tuple(info if info is not None else discord.version_info)
    if version[:2] < MIN_DISCORD_VERSION:
        raise RuntimeError(f"discord.py 2.5 이상이 필요합니다(현재 {'.'.join(map(str, version[:3]))})")


class KoreanTranslator(app_commands.Translator):
    """커맨드 이름 로컬라이즈(`upcoming_ipos` → `상장예정`)."""

    def __init__(self, names: Mapping[str, str]) -> None:
        self._names = dict(names)

    async def translate(
        self,
        string: app_commands.locale_str,
        locale: discord.Locale,
        context: app_commands.TranslationContextTypes,
    ) -> str | None:
        if locale is not discord.Locale.korean:
            return None
        if context.location is not app_commands.TranslationContextLocation.command_name:
            return None
        return self._names.get(string.message)


class Services:
    """DB·어댑터·서비스 묶음. 채널이 필요 없는 부분만 미리 만든다."""

    def __init__(self, settings: Settings, clock: Clock, conn: sqlite3.Connection | None = None, **overrides: Any):
        self.settings = settings
        self.clock = clock
        self.conn = conn if conn is not None else open_database(settings.db_path)
        self.krx = overrides.get("krx") or HttpKrxClient(settings.krx_base_url, settings.krx_auth_key)
        self.dart = overrides.get("dart") or HttpDartClient(settings.dart_api_key)
        self.llm = overrides.get("llm") or AnthropicLlmClient(
            settings.anthropic_api_key,
            summary_model=settings.anthropic_summary_model,
            parse_model=settings.anthropic_parse_model,
        )
        self.quotes = QuoteService(self.krx, self.conn, clock)
        self.calendar = KrxCalendar(source=CalendarRepo(self.conn))
        self.digest = IpoDigestService(self.dart, self.conn, clock, settings.ipo_range_days)
        self.summaries = SummaryService(self.llm, self.dart, self.conn, clock, settings.summary_retry_days)
        self.predictions = PredictionService(
            PredictionParser(self.llm),
            self.quotes,
            self.conn,
            clock,
            self.calendar,
            lookback=settings.base_lookback_trading_days,
            judge_time=settings.judge_time,
        )

    def scheduler_jobs(self, ipo_channel: discord.abc.Messageable, result_channel: discord.abc.Messageable) -> list[Job]:
        weekly = WeeklyService(
            self.conn,
            self.digest,
            ipo_channel,
            self.clock,
            WeeklySchedule.from_settings(self.settings),
            summaries=self.summaries,
        )
        judging = JudgingService(
            self.conn,
            self.quotes,
            self.calendar,
            result_channel,
            self.clock,
            PolicyConfig.from_settings(self.settings),
        )
        # 순서: 주간 게시 → 판정 → 공지. 각각 SchedulerCog.tick에서 격리된다.
        return [
            ("weekly", weekly.run_weekly),
            ("judging", judging.run_judging),
            ("announcements", judging.run_announcements),
        ]

    async def aclose(self) -> None:
        for client in (self.krx, self.dart, self.llm):
            close = getattr(client, "aclose", None)
            if close is not None:
                await close()
        self.conn.close()


class IpoBot(commands.Bot):
    def __init__(self, settings: Settings, clock: Clock | None = None, services: Services | None = None) -> None:
        # 특권 intent 없음: 슬래시 커맨드·컴포넌트 상호작용만 쓴다.
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default(), help_command=None)
        self.settings = settings
        self.clock = clock or SystemClock()
        alerts.set_default(OperatorAlerts(self.clock))  # Gate3 F8: KST 달력 날짜 기준 kind별 1회 경보
        self.drafts = DraftStore()
        self._services = services
        self.tree.on_error = self.on_app_command_error

    @property
    def services(self) -> Services:
        if self._services is None:
            self._services = Services(self.settings, self.clock)
        return self._services

    @property
    def guild(self) -> discord.Object:
        return discord.Object(id=self.settings.discord_guild_id)

    async def setup_hook(self) -> None:
        svc = self.services
        await self.add_cog(IpoCog(svc.digest, svc.summaries, self.clock), guild=self.guild)
        await self.add_cog(PredictCog(svc.predictions, self.drafts), guild=self.guild)
        await self.add_cog(SchedulerCog(self, self.scheduler_jobs, self.settings.scheduler_tick_seconds))
        await self.tree.set_translator(KoreanTranslator(KOREAN_COMMAND_NAMES))
        synced = await self.tree.sync(guild=self.guild)
        log.info("길드 커맨드 sync 완료 guild=%s commands=%s", self.settings.discord_guild_id, [c.name for c in synced])

    async def resolve_channel(self, channel_id: int) -> discord.abc.Messageable:
        channel = self.get_channel(channel_id)
        if channel is None:
            channel = await self.fetch_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            raise TypeError(f"메시지를 보낼 수 없는 채널입니다: {channel_id}")
        return channel

    async def scheduler_jobs(self) -> list[Job]:
        ipo_channel = await self.resolve_channel(self.settings.ipo_channel_id)
        result_channel = await self.resolve_channel(self.settings.result_channel_id)
        return self.services.scheduler_jobs(ipo_channel, result_channel)

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        await report_interaction_error(interaction, error)

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        await expired_fallback(interaction, self.drafts)

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            if self._services is not None:
                await self._services.aclose()
                self._services = None


def run(env: Mapping[str, str] | None = None) -> int:
    """`python -m ipobot run`. 설정 오류는 ConfigError로 올린다(호출자가 종료 코드 2로 처리)."""
    require_discord_version()
    settings = load_settings(env)
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log.info("ipobot 시작 설정=%s", settings.masked_summary())
    bot = IpoBot(settings, SystemClock())
    bot.run(settings.discord_token, log_handler=None)
    return 0
