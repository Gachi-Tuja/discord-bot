"""상호작용 예외 처리: 로그 + 일반 ephemeral 실패 안내(응답 여부에 따라 followup/send_message), HTTP 오류 삼킴."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
from discord import app_commands

from ipobot.bot.app import IpoBot
from ipobot.bot.cogs.predict import PredictModal
from ipobot.bot.errors import INTERACTION_FAILED_TEXT, report_interaction_error
from tests.fakes.discord_channel import forbidden
from tests.unit.test_bot_app import make_services


def interaction(done: bool, send_error: Exception | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        response=SimpleNamespace(is_done=MagicMock(return_value=done), send_message=AsyncMock(side_effect=send_error)),
        followup=SimpleNamespace(send=AsyncMock(side_effect=send_error)),
    )


async def test_not_responded_uses_send_message_and_logs(caplog):
    inter = interaction(done=False)
    with caplog.at_level(logging.ERROR, logger="ipobot.bot.errors"):
        await report_interaction_error(inter, RuntimeError("boom"))
    inter.response.send_message.assert_awaited_once_with(INTERACTION_FAILED_TEXT, ephemeral=True)
    inter.followup.send.assert_not_awaited()
    assert any(r.exc_info and r.exc_info[0] is RuntimeError for r in caplog.records)


async def test_already_responded_uses_followup():
    inter = interaction(done=True)
    await report_interaction_error(inter, RuntimeError("boom"))
    inter.followup.send.assert_awaited_once_with(INTERACTION_FAILED_TEXT, ephemeral=True)
    inter.response.send_message.assert_not_awaited()


async def test_http_exception_while_reporting_is_swallowed():
    inter = interaction(done=True, send_error=forbidden())
    await report_interaction_error(inter, RuntimeError("boom"))  # 예외가 올라오지 않는다
    inter.followup.send.assert_awaited_once()


async def test_interaction_responded_race_falls_back_to_followup():
    # is_done()는 False였지만 그 사이 다른 경로가 응답해 send_message가 InteractionResponded를 낸다.
    inter = interaction(done=False)
    inter.response.send_message.side_effect = discord.InteractionResponded(MagicMock())
    await report_interaction_error(inter, RuntimeError("boom"))
    inter.followup.send.assert_awaited_once_with(INTERACTION_FAILED_TEXT, ephemeral=True)


async def test_interaction_responded_then_followup_http_failure_is_swallowed(caplog):
    inter = interaction(done=False)
    inter.response.send_message.side_effect = discord.InteractionResponded(MagicMock())
    inter.followup.send.side_effect = forbidden()
    with caplog.at_level(logging.WARNING, logger="ipobot.bot.errors"):
        await report_interaction_error(inter, RuntimeError("boom"))  # 예외가 올라오지 않는다
    inter.followup.send.assert_awaited_once()
    assert any(r.levelno == logging.WARNING for r in caplog.records if r.name == "ipobot.bot.errors")


async def test_predict_modal_on_error_reports():
    modal = PredictModal(AsyncMock())
    inter = interaction(done=True)
    await modal.on_error(inter, RuntimeError("boom"))
    inter.followup.send.assert_awaited_once_with(INTERACTION_FAILED_TEXT, ephemeral=True)


async def test_bot_tree_on_error_reports(base_env, fake_clock):
    svc = make_services(base_env, fake_clock)
    bot = IpoBot(svc.settings, fake_clock, svc)
    inter = interaction(done=False)
    try:
        await bot.tree.on_error(inter, app_commands.AppCommandError("boom"))
        inter.response.send_message.assert_awaited_once_with(INTERACTION_FAILED_TEXT, ephemeral=True)
    finally:
        await bot.close()
