"""S15 확인 뷰(R6, R25): 작성자 검사, timeout=600, 확정·재시도·취소, 재시작 후 만료 폴백."""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from ipobot.bot.formatting import CONFIRM_RESULT_SCHEDULE, REJECT_DUPLICATE, REJECT_KRX_UNPUBLISHED
from ipobot.bot.views import (
    BUSY_TEXT,
    CANCELLED_TEXT,
    EXPIRED_TEXT,
    NOT_AUTHOR_TEXT,
    ConfirmView,
    DraftStore,
    confirm_custom_id,
    draft_id_of,
    expired_fallback,
    preview_text,
)
from ipobot.domain.models import DailyQuote, ParsedPrediction, PredictionTarget
from ipobot.services.predictions import Confirmed, Draft, Rejected

AUTHOR = 1001
OTHER = 2002


def make_draft() -> Draft:
    targets = tuple(
        PredictionTarget(h, 30_000 + h * 1_000, 300_000_000_000 + h * 10**10, f"{h}p", f"{h}c") for h in (1, 3, 6, 12)
    )
    parsed = ParsedPrediction("가나다", "성장성이 좋다", targets)
    base = DailyQuote(date(2026, 2, 2), "KOSDAQ", "123456", "가나다", 28_000, 27_500, 1000, 280_000_000_000, 10_000_000)
    dated = tuple(zip(targets, (date(2026, 3, 2), date(2026, 5, 4), date(2026, 8, 3), date(2027, 2, 2)), strict=True))
    return Draft(str(AUTHOR), "작성자", "원문", parsed, base, dated, ("⚠️ 경고 하나",))


def interaction(user_id: int = AUTHOR, *, custom_id: str | None = None, done: bool = False) -> SimpleNamespace:
    response = SimpleNamespace(
        send_message=AsyncMock(),
        defer=AsyncMock(),
        edit_message=AsyncMock(),
        is_done=MagicMock(return_value=done),
    )
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        response=response,
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
        type=discord.InteractionType.component,
        data={"custom_id": custom_id} if custom_id is not None else {},
    )


def make_view(confirm=None):
    store = DraftStore()
    draft = make_draft()
    draft_id = store.put(draft)
    confirm = confirm or AsyncMock(return_value=Confirmed(7, draft))
    return ConfirmView(draft_id, AUTHOR, store, confirm), store, draft_id, confirm


async def test_timeout_is_600_and_custom_ids_carry_draft_id():
    view, _, draft_id, _ = make_view()
    assert view.timeout == 600
    assert view.confirm_button.custom_id == confirm_custom_id(draft_id) == f"ipobot:confirm:{draft_id}"
    assert draft_id_of(view.cancel_button.custom_id) == draft_id
    assert draft_id_of("other:confirm:x") is None


async def test_interaction_check_author_true_other_false_with_ephemeral():
    view, *_ = make_view()
    author = interaction(AUTHOR)
    assert await view.interaction_check(author) is True
    author.response.send_message.assert_not_awaited()

    other = interaction(OTHER)
    assert await view.interaction_check(other) is False
    other.response.send_message.assert_awaited_once_with(NOT_AUTHOR_TEXT, ephemeral=True)


async def test_confirm_success_disables_buttons_and_posts_public_followup():
    view, store, draft_id, confirm = make_view()
    inter = interaction()
    await view.on_confirm(inter)
    confirm.assert_awaited_once()
    inter.response.defer.assert_awaited_once()
    assert all(item.disabled for item in view.children)
    inter.edit_original_response.assert_awaited_once_with(view=view)
    (text,), kwargs = inter.followup.send.await_args
    assert CONFIRM_RESULT_SCHEDULE in text and "ephemeral" not in kwargs
    assert store.get(draft_id) is None and store.knows(draft_id)
    # 같은 버튼 재클릭: 초안이 없으므로 두 번 확정하지 않는다.
    again = interaction()
    await view.on_confirm(again)
    assert confirm.await_count == 1
    again.response.send_message.assert_awaited_once_with(EXPIRED_TEXT, ephemeral=True)


async def test_retryable_rejection_keeps_draft_and_buttons():
    view, store, draft_id, confirm = make_view(AsyncMock(return_value=Rejected(REJECT_KRX_UNPUBLISHED)))
    inter = interaction()
    await view.on_confirm(inter)
    inter.followup.send.assert_awaited_once_with(REJECT_KRX_UNPUBLISHED, ephemeral=True)
    assert store.get(draft_id) is not None
    assert not any(item.disabled for item in view.children)


async def test_terminal_rejection_drops_draft():
    view, store, draft_id, _ = make_view(AsyncMock(return_value=Rejected(REJECT_DUPLICATE)))
    inter = interaction()
    await view.on_confirm(inter)
    inter.followup.send.assert_awaited_once_with(REJECT_DUPLICATE, ephemeral=True)
    assert store.get(draft_id) is None
    assert all(item.disabled for item in view.children)


async def test_cancel_resolves_without_saving():
    view, store, draft_id, confirm = make_view()
    inter = interaction()
    await view.on_cancel(inter)
    inter.response.edit_message.assert_awaited_once_with(content=CANCELLED_TEXT, view=None)
    confirm.assert_not_awaited()
    assert store.get(draft_id) is None


async def test_on_timeout_disables_and_expires_draft():
    view, store, draft_id, _ = make_view()
    view.message = SimpleNamespace(content="미리보기", edit=AsyncMock())
    await view.on_timeout()
    assert store.get(draft_id) is None and not store.knows(draft_id)
    assert all(item.disabled for item in view.children)
    view.message.edit.assert_awaited_once()
    # 타임아웃 뒤 클릭은 폴백이 「만료됨」으로 응답한다.
    inter = interaction(custom_id=confirm_custom_id(draft_id))
    assert await expired_fallback(inter, store) is True


async def test_expired_fallback_for_unknown_draft_after_restart():
    store = DraftStore()
    inter = interaction(OTHER, custom_id="ipobot:confirm:xyz")
    assert await expired_fallback(inter, store) is True
    inter.response.send_message.assert_awaited_once_with(EXPIRED_TEXT, ephemeral=True)
    assert "만료됨" in EXPIRED_TEXT


async def test_no_fallback_when_draft_in_store_or_unrelated():
    store = DraftStore()
    draft_id = store.put(make_draft())
    live = interaction(custom_id=confirm_custom_id(draft_id))
    assert await expired_fallback(live, store) is False
    live.response.send_message.assert_not_awaited()

    unrelated = interaction(custom_id="someone_else:button")
    assert await expired_fallback(unrelated, store) is False

    command = interaction(custom_id="ipobot:confirm:xyz")
    command.type = discord.InteractionType.application_command
    assert await expired_fallback(command, store) is False
    command.response.send_message.assert_not_awaited()


def test_preview_lists_base_four_horizons_warnings_and_schedule():
    text = preview_text(make_draft())
    assert "123456" in text and "2026-02-02" in text and "28,000원" in text
    for h in (1, 3, 6, 12):
        assert f"{h}개월" in text
    assert "⚠️ 경고 하나" in text and "⚠️ ⚠️" not in text
    assert CONFIRM_RESULT_SCHEDULE in text


@pytest.mark.parametrize("custom_id", ["ipobot:", "ipobot:confirm", "ipobot:confirm:"])
def test_draft_id_of_rejects_malformed(custom_id):
    assert draft_id_of(custom_id) is None


async def test_cancel_while_confirm_in_flight_is_refused_and_prediction_saved():
    gate = asyncio.Event()
    saved = []

    async def confirm(draft):
        await gate.wait()
        saved.append(draft)
        return Confirmed(7, draft)

    view, store, draft_id, _ = make_view(confirm)
    confirming, cancelling = interaction(), interaction()
    task = asyncio.create_task(view.on_confirm(confirming))
    await asyncio.sleep(0)
    await view.on_cancel(cancelling)
    cancelling.response.send_message.assert_awaited_once_with(BUSY_TEXT, ephemeral=True)
    cancelling.response.edit_message.assert_not_awaited()
    gate.set()
    await task
    assert len(saved) == 1
    (text,), _ = confirming.followup.send.await_args
    assert CONFIRM_RESULT_SCHEDULE in text


async def test_confirm_after_cancel_does_not_save():
    view, store, draft_id, confirm = make_view()
    await view.on_cancel(interaction())
    later = interaction()
    await view.on_confirm(later)
    confirm.assert_not_awaited()
    later.response.send_message.assert_awaited_once_with(EXPIRED_TEXT, ephemeral=True)
    # 이미 끝난 초안에 대한 두 번째 취소도 메시지를 「취소했습니다」로 바꾸지 않는다.
    again = interaction()
    await view.on_cancel(again)
    again.response.edit_message.assert_not_awaited()
    again.response.send_message.assert_awaited_once_with(EXPIRED_TEXT, ephemeral=True)


def test_preview_with_huge_texts_fits_discord_message():
    from dataclasses import replace

    draft = make_draft()
    huge = "가" * 5000
    parsed = replace(draft.parsed, reason=huge)
    draft = replace(draft, parsed=parsed, discord_user_name=huge, warnings=tuple(huge for _ in range(8)))
    assert len(preview_text(draft)) <= 2000


async def test_long_rejection_message_is_clipped_to_2000():
    view, *_ = make_view(AsyncMock(return_value=Rejected("거" * 5000)))
    inter = interaction()
    await view.on_confirm(inter)
    (text,), kwargs = inter.followup.send.await_args
    assert len(text) <= 2000 and kwargs == {"ephemeral": True}


async def test_unexpected_confirm_error_is_generic_not_krx_failure():
    from ipobot.bot.errors import INTERACTION_FAILED_TEXT
    from ipobot.bot.formatting import REJECT_KRX_FAILED

    view, store, draft_id, _ = make_view(AsyncMock(side_effect=OverflowError("int too big")))
    inter = interaction()
    await view.on_confirm(inter)
    inter.followup.send.assert_awaited_once_with(INTERACTION_FAILED_TEXT, ephemeral=True)
    assert inter.followup.send.await_args.args[0] != REJECT_KRX_FAILED
