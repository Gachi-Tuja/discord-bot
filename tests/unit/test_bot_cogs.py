"""S15 cog: `/upcoming_ipos`(R16·R18·CRIT-P1-F4)와 `/predict`(Modal → draft → 미리보기·확인 뷰)."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from ipobot.bot.cogs.ipo import IpoCog
from ipobot.bot.cogs.predict import LLM_FAILED_TEXT, PREVIEW_POSTED_TEXT, PredictCog, PredictModal
from ipobot.bot.formatting import NO_UPCOMING_IPOS, REJECT_STOCK_NOT_FOUND
from ipobot.bot.views import ConfirmView, DraftStore
from ipobot.clock import KST
from ipobot.services.ipo_digest import DART_UNAVAILABLE_TEXT, DartUnavailable
from ipobot.services.predictions import Rejected
from ipobot.services.summaries import LlmError
from tests.conftest import FakeClock
from tests.unit.test_views import make_draft


def interaction(user_id: int = 1001) -> SimpleNamespace:
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, display_name="작성자"),
        response=SimpleNamespace(defer=AsyncMock(), send_modal=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=1, content="x"))),
        edit_original_response=AsyncMock(),
    )


def ipo_cog(digest_side_effect=None, listings=()):
    digest = SimpleNamespace(build_digest=AsyncMock(return_value=list(listings), side_effect=digest_side_effect))
    summaries = SimpleNamespace(summaries_for=AsyncMock(return_value={}))
    clock = FakeClock(datetime(2026, 2, 6, 23, 30, tzinfo=KST))
    return IpoCog(digest, summaries, clock), digest, summaries


async def test_upcoming_ipos_dart_failure_sends_only_failure_notice():
    cog, digest, summaries = ipo_cog(DartUnavailable("down"))
    inter = interaction()
    await cog.upcoming_ipos.callback(cog, inter)
    inter.response.defer.assert_awaited_once()
    inter.followup.send.assert_awaited_once_with(DART_UNAVAILABLE_TEXT)
    summaries.summaries_for.assert_not_awaited()


async def test_upcoming_ipos_success_uses_today_kst_and_digest_parts():
    cog, digest, summaries = ipo_cog()
    inter = interaction()
    await cog.upcoming_ipos.callback(cog, inter)
    (today,), _ = digest.build_digest.await_args
    assert today.isoformat() == "2026-02-06"
    summaries.summaries_for.assert_awaited_once_with([])
    # 수집 성공·0건일 때만 0건 안내(R18). nonce 없이 followup으로 보낸다.
    inter.followup.send.assert_awaited_once_with(content=NO_UPCOMING_IPOS)


def predict_cog(result=None, side_effect=None):
    service = SimpleNamespace(
        draft=AsyncMock(return_value=result, side_effect=side_effect),
        confirm_draft=AsyncMock(),
    )
    store = DraftStore()
    return PredictCog(service, store), service, store


async def test_predict_command_opens_modal_that_forwards_text():
    cog, service, _ = predict_cog(Rejected(REJECT_STOCK_NOT_FOUND))
    inter = interaction()
    await cog.predict.callback(cog, inter)
    (modal,), _ = inter.response.send_modal.await_args
    assert isinstance(modal, PredictModal)
    assert modal.text.max_length == 2000
    modal.text._value = "가나다 1개월 3만원"
    submit = interaction()
    await modal.on_submit(submit)
    service.draft.assert_awaited_once_with("1001", "작성자", "가나다 1개월 3만원")


async def test_submission_rejected_is_ephemeral():
    cog, _, store = predict_cog(Rejected(REJECT_STOCK_NOT_FOUND))
    inter = interaction()
    await cog.handle_submission(inter, "text")
    inter.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
    inter.followup.send.assert_awaited_once_with(REJECT_STOCK_NOT_FOUND, ephemeral=True)
    inter.edit_original_response.assert_not_awaited()


async def test_submission_llm_error_is_reported():
    cog, _, _ = predict_cog(side_effect=LlmError("x"))
    inter = interaction()
    await cog.handle_submission(inter, "text")
    inter.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
    inter.followup.send.assert_awaited_once_with(LLM_FAILED_TEXT, ephemeral=True)


async def test_submission_draft_posts_preview_with_author_only_confirm_view():
    draft = make_draft()
    cog, service, store = predict_cog(draft)
    inter = interaction(1001)
    await cog.handle_submission(inter, "text")
    inter.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
    # 미룬(ephemeral) 응답은 안내로 바꾸고, 미리보기는 별도의 공개 메시지로 게시한다.
    inter.edit_original_response.assert_awaited_once_with(content=PREVIEW_POSTED_TEXT)
    inter.followup.send.assert_awaited_once()
    (text,), kwargs = inter.followup.send.await_args
    assert "ephemeral" not in kwargs
    view = kwargs["view"]
    assert isinstance(view, ConfirmView) and view.author_id == 1001
    assert kwargs["wait"] is True and view.message is inter.followup.send.return_value
    assert "123456" in text
    assert store.get(view.draft_id) is draft
    view.stop()


async def test_long_submission_rejection_is_clipped_to_2000():
    cog, _, _ = predict_cog(Rejected("거" * 5000))
    inter = interaction()
    await cog.handle_submission(inter, "text")
    (text,), kwargs = inter.followup.send.await_args
    assert len(text) <= 2000 and kwargs == {"ephemeral": True}
