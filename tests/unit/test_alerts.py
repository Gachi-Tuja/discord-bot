"""운영자 경보(S3c, Gate3 F5·F8): 종류별 KST 날짜당 1회."""

from __future__ import annotations

import logging
from datetime import datetime

from ipobot.clock import KST
from ipobot.services.alerts import OperatorAlerts


def test_operator_alert_once_per_day_per_kind(caplog, fake_clock):
    clock = fake_clock
    alerts = OperatorAlerts(clock)
    with caplog.at_level(logging.ERROR):
        assert alerts.alert("krx_auth", "401") is True
        assert alerts.alert("krx_auth", "401") is False
        assert alerts.alert("discord_forbidden", "x") is True
        clock.set(datetime(2026, 11, 13, 9, 0, tzinfo=KST))
        assert alerts.alert("krx_auth", "401") is True
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert errors == ["OPERATOR_ALERT krx_auth 401", "OPERATOR_ALERT discord_forbidden x",
                      "OPERATOR_ALERT krx_auth 401"]


def test_dedupe_is_per_kst_calendar_day_not_24h(fake_clock):
    alerts = OperatorAlerts(fake_clock)
    fake_clock.set(datetime(2026, 11, 12, 23, 59, tzinfo=KST))
    assert alerts.alert("krx_auth", "401") is True
    fake_clock.set(datetime(2026, 11, 13, 0, 0, tzinfo=KST))
    assert alerts.alert("krx_auth", "401") is True
