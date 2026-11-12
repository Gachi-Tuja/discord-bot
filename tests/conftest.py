from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from ipobot.clock import KST

FIXTURES = Path(__file__).parent / "fixtures"

REQUIRED_ENV = {
    "DISCORD_TOKEN": "test-token",
    "DISCORD_GUILD_ID": "111",
    "IPO_CHANNEL_ID": "222",
    "RESULT_CHANNEL_ID": "333",
    "ANTHROPIC_API_KEY": "test-anthropic",
    "DART_API_KEY": "test-dart",
    "KRX_AUTH_KEY": "test-krx",
}


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def set(self, now: datetime) -> None:
        self._now = now


@pytest.fixture
def base_env() -> dict[str, str]:
    return dict(REQUIRED_ENV)


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock(datetime(2026, 11, 12, 9, 0, tzinfo=KST))


@pytest.fixture
def load_fixture():
    def _load(name: str):
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    return _load
