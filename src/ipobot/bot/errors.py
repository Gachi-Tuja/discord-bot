"""상호작용 처리 중 예상하지 못한 예외의 공통 처리: 로그를 남기고 작성자에게 일반 실패 안내를 보낸다."""

from __future__ import annotations

import logging

import discord

log = logging.getLogger(__name__)

INTERACTION_FAILED_TEXT = "요청을 처리하는 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요"


async def report_interaction_error(interaction: discord.Interaction, error: BaseException) -> None:
    """예외를 기록하고 ephemeral 실패 안내를 보낸다. 이미 응답했으면 followup으로 보낸다. 전송 실패는 삼킨다."""
    log.error("상호작용 처리 실패", exc_info=(type(error), error, error.__traceback__))
    try:
        if interaction.response.is_done():
            await interaction.followup.send(INTERACTION_FAILED_TEXT, ephemeral=True)
        else:
            await interaction.response.send_message(INTERACTION_FAILED_TEXT, ephemeral=True)
    except discord.HTTPException:
        log.warning("실패 안내 전송 실패", exc_info=True)
