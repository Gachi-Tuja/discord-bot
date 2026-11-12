"""설치된 discord.py가 전송 헬퍼(R22)가 의존하는 계약을 지키는지 확인한다."""

import inspect

import discord
import discord.http


def test_send_accepts_nonce_but_not_enforce_nonce():
    params = inspect.signature(discord.abc.Messageable.send).parameters
    assert "nonce" in params
    assert "enforce_nonce" not in params


def test_message_payload_enforces_nonce():
    params = discord.http.handle_message_parameters(content="x", nonce="j1:0")
    payload = params.payload
    assert payload["nonce"] == "j1:0"
    assert payload["enforce_nonce"] is True


def test_version_floor():
    assert discord.version_info >= (2, 5)
