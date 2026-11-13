"""`/upcoming_ipos`(로컬라이즈 `상장예정`) — R16.

주간 게시와 같은 `build_digest(today)`·`digest_parts`를 쓴다. defer 후 followup으로 응답한다(nonce 대상 아님).
DART 수집에 실패하면 실패 안내만 보낸다(0건 안내 금지, CRIT-P1-F4).
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from ipobot.bot.formatting import digest_parts
from ipobot.clock import Clock, today_kst
from ipobot.services.ipo_digest import DART_UNAVAILABLE_TEXT, DartUnavailable, digest_items
from ipobot.services.weekly import DigestBuilder, SummarySource

log = logging.getLogger(__name__)

UPCOMING_IPOS = "upcoming_ipos"
# app_commands.Translator가 쓰는 한국어 커맨드 이름(locale ko)
KOREAN_COMMAND_NAMES = {UPCOMING_IPOS: "상장예정"}


class IpoCog(commands.Cog):
    def __init__(self, digest: DigestBuilder, summaries: SummarySource, clock: Clock) -> None:
        self.digest = digest
        self.summaries = summaries
        self.clock = clock

    @app_commands.command(name=UPCOMING_IPOS, description="향후 상장 예정 종목을 보여줍니다")
    async def upcoming_ipos(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        today = today_kst(self.clock)
        try:
            listings = await self.digest.build_digest(today)
        except DartUnavailable as exc:
            log.warning("upcoming_ipos DART 실패: %s", exc)
            await interaction.followup.send(DART_UNAVAILABLE_TEXT)
            return
        summaries = await self.summaries.summaries_for(listings)
        for part in digest_parts(digest_items(listings, summaries), today):
            await interaction.followup.send(**part.kwargs)
