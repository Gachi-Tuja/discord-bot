from datetime import time

import pytest

from ipobot import config
from ipobot.config import ConfigError, load_settings

REQUIRED_NAMES = [
    "DISCORD_TOKEN",
    "DISCORD_GUILD_ID",
    "IPO_CHANNEL_ID",
    "RESULT_CHANNEL_ID",
    "ANTHROPIC_API_KEY",
    "DART_API_KEY",
    "KRX_AUTH_KEY",
]


def test_defaults(base_env):
    s = load_settings(base_env)
    assert s.weekly_post_weekday == 4  # FRI
    assert s.weekly_post_time == time(18, 0)
    assert s.weekly_post_grace_hours == 6
    assert s.weekly_retry_backoff_minutes == 15
    assert s.ipo_range_days == 7
    assert s.base_lookback_trading_days == 3
    assert s.judge_time == time(9, 0)
    assert s.krx_poll_interval_minutes == 30
    assert s.krx_poll_deadline == time(15, 0)
    assert s.judge_max_attempts == 5
    assert s.judge_stale_business_days == 10
    assert s.summary_retry_days == 3
    assert s.scheduler_tick_seconds == 60
    assert s.log_level == "INFO"
    assert s.krx_base_url == "https://data-dbg.krx.co.kr/svc/apis"
    assert s.db_path == "./data/ipobot.sqlite3"
    assert s.anthropic_summary_model == config.DEFAULT_SUMMARY_MODEL
    assert s.anthropic_parse_model == config.DEFAULT_PARSE_MODEL
    assert s.discord_guild_id == 111


@pytest.mark.parametrize("name", REQUIRED_NAMES)
def test_missing_required_names_variable(base_env, name):
    del base_env[name]
    with pytest.raises(ConfigError, match=name):
        load_settings(base_env)


def test_missing_required_message_format(base_env):
    del base_env["DISCORD_TOKEN"]
    with pytest.raises(ConfigError) as info:
        load_settings(base_env)
    assert str(info.value) == "필수 환경변수가 없습니다: DISCORD_TOKEN"


def test_clock_timezone_comes_from_config():
    from ipobot import clock

    assert clock.KST.key == config.TIMEZONE_NAME == "Asia/Seoul"


@pytest.mark.parametrize("name", REQUIRED_NAMES)
def test_blank_required_is_missing(base_env, name):
    base_env[name] = "   "
    with pytest.raises(ConfigError, match=name):
        load_settings(base_env)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DISCORD_GUILD_ID", "abc"),
        ("IPO_CHANNEL_ID", "0"),
        ("WEEKLY_POST_WEEKDAY", "FRIDAY"),
        ("WEEKLY_POST_TIME", "6pm"),
        ("WEEKLY_POST_TIME", "24:00"),
        ("JUDGE_TIME", "9:00"),
        ("KRX_POLL_INTERVAL_MINUTES", "0"),
        ("JUDGE_MAX_ATTEMPTS", "0"),
        ("JUDGE_STALE_BUSINESS_DAYS", "0"),
        ("JUDGE_STALE_BUSINESS_DAYS", "-1"),
        ("LOG_LEVEL", "LOUD"),
    ],
)
def test_bad_format_rejected(base_env, name, value):
    base_env[name] = value
    with pytest.raises(ConfigError, match=name):
        load_settings(base_env)


def test_overrides_parsed(base_env):
    base_env.update(
        WEEKLY_POST_WEEKDAY="mon",
        WEEKLY_POST_TIME="07:30",
        JUDGE_STALE_BUSINESS_DAYS="1",
        KRX_BASE_URL="https://example.test/apis/",
        LOG_LEVEL="debug",
    )
    s = load_settings(base_env)
    assert s.weekly_post_weekday == 0
    assert s.weekly_post_time == time(7, 30)
    assert s.judge_stale_business_days == 1
    assert s.krx_base_url == "https://example.test/apis"
    assert s.log_level == "DEBUG"


@pytest.mark.parametrize("judge_time", ["15:00", "16:00"])
def test_judge_time_must_precede_deadline(base_env, judge_time):
    base_env["JUDGE_TIME"] = judge_time
    with pytest.raises(ConfigError, match="JUDGE_TIME"):
        load_settings(base_env)


def test_backoff_must_be_less_than_grace(base_env):
    base_env["WEEKLY_POST_GRACE_HOURS"] = "1"
    base_env["WEEKLY_RETRY_BACKOFF_MINUTES"] = "60"
    with pytest.raises(ConfigError, match="WEEKLY_RETRY_BACKOFF_MINUTES"):
        load_settings(base_env)
    base_env["WEEKLY_RETRY_BACKOFF_MINUTES"] = "59"
    assert load_settings(base_env).weekly_retry_backoff_minutes == 59


def test_no_kis_or_removed_settings(base_env):
    fields = set(load_settings(base_env).__dict__)
    assert not any("kis" in f for f in fields)
    for removed in ("price_source", "close_confirm_time"):
        assert removed not in fields


def test_settings_frozen(base_env):
    s = load_settings(base_env)
    with pytest.raises(AttributeError):
        s.judge_time = time(10, 0)  # type: ignore[misc]


def test_masked_summary_hides_secrets(base_env):
    summary = load_settings(base_env).masked_summary()
    for key in ("discord_token", "anthropic_api_key", "dart_api_key", "krx_auth_key"):
        assert summary[key] == "***"
    assert "test-token" not in repr(summary)


def test_code_constants():
    assert config.SEND_INWINDOW_RETRY_DELAYS == (2, 5, 10)
    assert config.SEND_CALL_TIMEOUT == 20
    assert config.SEND_INWINDOW_BUDGET == 90
    assert config.NONCE_MAX_LENGTH == 25
