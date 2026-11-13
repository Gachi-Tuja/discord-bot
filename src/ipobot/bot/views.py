"""예측 미리보기·확인 뷰(R6, R25)와 재시작 후 클릭 만료 폴백.

초안은 프로세스 메모리(`DraftStore`)에만 둔다(§1 원칙 2). 확인 버튼은 작성자만 누를 수 있고, 600초가 지나면
버튼을 비활성화하고 초안을 버린다. 저장소에 없는 `ipobot:` 버튼 클릭(재시작·타임아웃 이후)에는
`expired_fallback`이 ephemeral 「만료됨」으로 응답한다.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

import discord

from ipobot.bot.formatting import (
    CONFIRM_RESULT_SCHEDULE,
    REJECT_KRX_FAILED,
    REJECT_KRX_UNPUBLISHED,
    format_krw,
    format_won,
)
from ipobot.services.predictions import Confirmed, Draft, Rejected

log = logging.getLogger(__name__)

VIEW_TIMEOUT_SECONDS = 600
CUSTOM_ID_PREFIX = "ipobot:"
EXPIRED_TEXT = "만료됨. /predict로 다시 제출해 주세요"
EXPIRED_PREVIEW_SUFFIX = "⌛ 만료됨. 저장되지 않았습니다"
NOT_AUTHOR_TEXT = "작성자만 확인·취소할 수 있습니다"
CANCELLED_TEXT = "취소했습니다. 예측은 저장되지 않았습니다"
BUSY_TEXT = "처리 중입니다"
REASON_PREVIEW_LIMIT = 500
# 다시 눌러 볼 수 있는 일시적 거부. 이 경우 초안과 버튼을 유지한다.
RETRYABLE_REJECTIONS = frozenset({REJECT_KRX_FAILED, REJECT_KRX_UNPUBLISHED})

ConfirmFn = Callable[[Draft], Awaitable["Confirmed | Rejected"]]


def confirm_custom_id(draft_id: str) -> str:
    return f"{CUSTOM_ID_PREFIX}confirm:{draft_id}"


def cancel_custom_id(draft_id: str) -> str:
    return f"{CUSTOM_ID_PREFIX}cancel:{draft_id}"


def draft_id_of(custom_id: str) -> str | None:
    """`ipobot:<action>:<draft_id>`에서 draft_id를 꺼낸다. 형식이 아니면 None."""
    if not custom_id.startswith(CUSTOM_ID_PREFIX):
        return None
    parts = custom_id.split(":", 2)
    if len(parts) != 3 or not parts[2]:
        return None
    return parts[2]


class DraftStore:
    """미확정 초안 저장소(메모리). 처리 중·처리 끝난 id는 기억해 폴백이 잘못 응답하지 않게 한다."""

    def __init__(self) -> None:
        self._drafts: dict[str, Draft] = {}
        self._handled: set[str] = set()

    def put(self, draft: Draft) -> str:
        while True:
            draft_id = secrets.token_hex(8)
            if draft_id not in self._drafts and draft_id not in self._handled:
                break
        self._drafts[draft_id] = draft
        return draft_id

    def get(self, draft_id: str) -> Draft | None:
        return self._drafts.get(draft_id)

    def resolve(self, draft_id: str) -> None:
        """확정·취소로 끝난 초안. 해당 메시지에는 더 이상 버튼이 없다."""
        self._drafts.pop(draft_id, None)
        self._handled.add(draft_id)

    def expire(self, draft_id: str) -> None:
        """타임아웃. 이후 클릭은 폴백이 「만료됨」으로 응답한다."""
        self._drafts.pop(draft_id, None)

    def knows(self, draft_id: str) -> bool:
        return draft_id in self._drafts or draft_id in self._handled

    def __contains__(self, draft_id: object) -> bool:
        return draft_id in self._drafts


def preview_text(draft: Draft) -> str:
    """미리보기: 종목·기준일·기준값·4시점·이유·경고·공지 시점 안내."""
    base = draft.base
    lines = [
        f"**{base.corp_name}**({base.stock_code}, {base.market}) 예측 미리보기 — 작성자 {draft.discord_user_name}",
        f"기준일 {base.trade_date.isoformat()} (KRX 최신 공개 거래일)",
        f"기준 종가 {format_won(base.close_price)} · 기준 시총 {format_krw(base.market_cap)}",
        "",
    ]
    for target, target_date in draft.targets:
        lines.append(
            f"- {target.horizon_months}개월 (목표일 {target_date.isoformat()}): "
            f"주가 {format_won(target.pred_price)} · 시총 {format_krw(target.pred_market_cap)}"
        )
    reason = draft.parsed.reason.strip()
    if len(reason) > REASON_PREVIEW_LIMIT:
        reason = reason[: REASON_PREVIEW_LIMIT - 1] + "…"
    lines += ["", f"이유: {reason}"]
    lines += [w if w.startswith("⚠️") else f"⚠️ {w}" for w in draft.warnings]
    lines += ["", CONFIRM_RESULT_SCHEDULE, "확인을 누르면 수정·삭제할 수 없습니다."]
    return "\n".join(lines)


def confirmed_text(confirmed: Confirmed) -> str:
    base = confirmed.draft.base
    return (
        f"✅ {confirmed.draft.discord_user_name}님의 {base.corp_name}({base.stock_code}) 예측이 확정되었습니다 "
        f"(기준일 {base.trade_date.isoformat()}, 기준 종가 {format_won(base.close_price)}).\n"
        f"{CONFIRM_RESULT_SCHEDULE}"
    )


class ConfirmView(discord.ui.View):
    def __init__(self, draft_id: str, author_id: int, store: DraftStore, confirm: ConfirmFn) -> None:
        super().__init__(timeout=VIEW_TIMEOUT_SECONDS)
        self.draft_id = draft_id
        self.author_id = author_id
        self.store = store
        self._confirm = confirm
        self._busy = False
        self.message: Any = None  # 미리보기 메시지(타임아웃 시 수정)
        self.confirm_button: discord.ui.Button[ConfirmView] = discord.ui.Button(
            label="확인", style=discord.ButtonStyle.success, custom_id=confirm_custom_id(draft_id)
        )
        self.cancel_button: discord.ui.Button[ConfirmView] = discord.ui.Button(
            label="취소", style=discord.ButtonStyle.secondary, custom_id=cancel_custom_id(draft_id)
        )
        self.confirm_button.callback = self.on_confirm
        self.cancel_button.callback = self.on_cancel
        self.add_item(self.confirm_button)
        self.add_item(self.cancel_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message(NOT_AUTHOR_TEXT, ephemeral=True)
        return False

    def _disable(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    async def on_confirm(self, interaction: discord.Interaction) -> None:
        draft = self.store.get(self.draft_id)
        if draft is None or self._busy:
            await interaction.response.send_message(EXPIRED_TEXT if draft is None else BUSY_TEXT, ephemeral=True)
            return
        self._busy = True
        await interaction.response.defer()
        try:
            result = await self._confirm(draft)
        except Exception:
            log.exception("예측 확정 실패 draft=%s", self.draft_id)
            self._busy = False
            await interaction.followup.send(REJECT_KRX_FAILED, ephemeral=True)
            return
        if isinstance(result, Rejected) and result.message in RETRYABLE_REJECTIONS:
            self._busy = False
            await interaction.followup.send(result.message, ephemeral=True)
            return
        self.store.resolve(self.draft_id)
        self.stop()
        self._disable()
        await interaction.edit_original_response(view=self)
        if isinstance(result, Confirmed):
            await interaction.followup.send(confirmed_text(result))
        else:
            await interaction.followup.send(result.message, ephemeral=True)

    async def on_cancel(self, interaction: discord.Interaction) -> None:
        # 확정이 진행 중이면 취소를 거부한다(저장 여부를 거짓으로 알리지 않는다). 그렇지 않으면 수정 전에
        # 먼저 초안을 정리해, 응답을 기다리는 사이에 들어온 확인 클릭이 저장하지 못하게 한다.
        if self._busy:
            await interaction.response.send_message(BUSY_TEXT, ephemeral=True)
            return
        if self.store.get(self.draft_id) is None:
            await interaction.response.send_message(EXPIRED_TEXT, ephemeral=True)
            return
        self.store.resolve(self.draft_id)
        self.stop()
        await interaction.response.edit_message(content=CANCELLED_TEXT, view=None)

    async def on_timeout(self) -> None:
        self.store.expire(self.draft_id)
        self._disable()
        if self.message is None:
            return
        try:
            await self.message.edit(content=f"{self.message.content}\n\n{EXPIRED_PREVIEW_SUFFIX}", view=self)
        except discord.HTTPException:
            log.warning("만료 표시 실패 draft=%s", self.draft_id, exc_info=True)


async def expired_fallback(interaction: discord.Interaction, store: DraftStore) -> bool:
    """재시작·타임아웃 뒤의 `ipobot:` 버튼 클릭에 ephemeral 「만료됨」으로 응답한다. 응답했으면 True."""
    if interaction.type is not discord.InteractionType.component:
        return False
    data = interaction.data or {}
    draft_id = draft_id_of(str(data.get("custom_id", "")))
    if draft_id is None or store.knows(draft_id):
        return False
    if interaction.response.is_done():
        return False
    await interaction.response.send_message(EXPIRED_TEXT, ephemeral=True)
    return True
