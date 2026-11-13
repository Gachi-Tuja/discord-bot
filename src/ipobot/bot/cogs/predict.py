"""`/predict`: Modal → LLM 파싱·금액 교차검증 → KRX 기준값 미리보기 → 작성자 확인 → 불변 저장.

파싱·기준값·중복 검사는 `PredictionService.draft`, 확정은 `confirm_draft`가 한다. 이 cog는 디스코드 입출력만 맡는다.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import commands

from ipobot.bot.errors import report_interaction_error
from ipobot.bot.formatting import clip_content
from ipobot.bot.views import ConfirmView, DraftStore, preview_text
from ipobot.services.predictions import Draft, PredictionService
from ipobot.services.summaries import LlmError

log = logging.getLogger(__name__)

TEXT_MAX_LENGTH = 2000
LLM_FAILED_TEXT = "예측 내용을 해석하는 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요"
PREVIEW_POSTED_TEXT = "미리보기를 채널에 게시했습니다"


class PredictModal(discord.ui.Modal, title="주가 예측 제출"):
    text: discord.ui.TextInput[PredictModal] = discord.ui.TextInput(
        label="예측 내용",
        style=discord.TextStyle.paragraph,
        placeholder="종목, 1·3·6·12개월 뒤 주가와 시가총액, 이유를 적어 주세요",
        max_length=TEXT_MAX_LENGTH,
    )

    def __init__(self, on_text: Callable[[discord.Interaction, str], Awaitable[None]]) -> None:
        super().__init__()
        self._on_text = on_text

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._on_text(interaction, self.text.value)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await report_interaction_error(interaction, error)


class PredictCog(commands.Cog):
    def __init__(self, service: PredictionService, store: DraftStore) -> None:
        self.service = service
        self.store = store

    @app_commands.command(name="predict", description="상장 종목 주가·시가총액 예측을 제출합니다")
    async def predict(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(PredictModal(self.handle_submission))

    async def handle_submission(self, interaction: discord.Interaction, text: str) -> None:
        # LLM·KRX 호출이 3초를 넘을 수 있어 먼저 응답을 미룬다. 미룬 응답은 ephemeral이라 거부·오류는
        # 작성자에게만 보인다. 미리보기는 별도 공개 메시지로 게시한다(타인 클릭 거부를 위해).
        await interaction.response.defer(thinking=True, ephemeral=True)
        user = interaction.user
        try:
            result = await self.service.draft(str(user.id), user.display_name, text)
        except LlmError as exc:
            log.warning("예측 파싱 LLM 실패 user=%s: %s", user.id, exc)
            await interaction.followup.send(LLM_FAILED_TEXT, ephemeral=True)
            return
        if not isinstance(result, Draft):
            await interaction.followup.send(clip_content(result.message), ephemeral=True)
            return
        draft_id = self.store.put(result)
        view = ConfirmView(draft_id, user.id, self.store, self.service.confirm_draft)
        await interaction.edit_original_response(content=PREVIEW_POSTED_TEXT)
        view.message = await interaction.followup.send(preview_text(result), view=view, wait=True)
