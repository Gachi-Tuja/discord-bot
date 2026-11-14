"""환경변수 설정. 필수값 누락·형식 오류·교차 검증 위반은 시작 시 즉시 실패한다(§7)."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time

# 코드 상수(§7, Part B)
TIMEZONE_NAME = "Asia/Seoul"
SEND_INWINDOW_RETRY_DELAYS: tuple[int, ...] = (2, 5, 10)
SEND_CALL_TIMEOUT = 20  # 초: channel.send 1회 상한
SEND_INWINDOW_BUDGET = 90  # 초: 파트별 같은 nonce 재시도 wall-clock 예산
NONCE_MAX_LENGTH = 25
DEFAULT_SUMMARY_MODEL = "claude-haiku-4-5"
DEFAULT_PARSE_MODEL = "claude-sonnet-4-5"

WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class ConfigError(ValueError):
    """설정 오류. 메시지에 문제 변수명을 담는다."""


@dataclass(frozen=True)
class Settings:
    discord_token: str
    discord_guild_id: int
    ipo_channel_id: int
    result_channel_id: int
    anthropic_api_key: str
    dart_api_key: str
    krx_auth_key: str
    anthropic_summary_model: str
    anthropic_parse_model: str
    krx_base_url: str
    db_path: str
    weekly_post_weekday: int  # 0=월 … 6=일
    weekly_post_time: time
    weekly_post_grace_hours: int
    weekly_retry_backoff_minutes: int
    ipo_range_days: int
    base_lookback_trading_days: int
    judge_time: time
    krx_poll_interval_minutes: int
    krx_poll_deadline: time
    judge_max_attempts: int
    judge_stale_business_days: int
    summary_retry_days: int
    scheduler_tick_seconds: int
    log_level: str

    def masked_summary(self) -> dict[str, object]:
        """기동 로그용 요약. 비밀값은 마스킹한다."""
        secret = {"discord_token", "anthropic_api_key", "dart_api_key", "krx_auth_key"}
        return {
            k: ("***" if k in secret else v) for k, v in self.__dict__.items()
        }


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"필수 환경변수가 없습니다: {name}")
    return value


def _optional(env: Mapping[str, str], name: str, default: str) -> str:
    value = env.get(name, "").strip()
    return value or default


def _int(name: str, raw: str, *, minimum: int | None = None) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name}는 정수여야 합니다: {raw!r}") from None
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name}는 {minimum} 이상이어야 합니다: {value}")
    return value


def _snowflake(env: Mapping[str, str], name: str) -> int:
    return _int(name, _required(env, name), minimum=1)


def _time(name: str, raw: str) -> time:
    m = _TIME_RE.match(raw)
    if not m:
        raise ConfigError(f"{name}는 HH:MM 형식이어야 합니다: {raw!r}")
    return time(int(m.group(1)), int(m.group(2)))


def _weekday(name: str, raw: str) -> int:
    upper = raw.upper()
    if upper not in WEEKDAYS:
        raise ConfigError(f"{name}는 {'/'.join(WEEKDAYS)} 중 하나여야 합니다: {raw!r}")
    return WEEKDAYS.index(upper)


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env

    def opt_int(name: str, default: int, minimum: int) -> int:
        return _int(name, _optional(env, name, str(default)), minimum=minimum)

    log_level = _optional(env, "LOG_LEVEL", "INFO").upper()
    if log_level not in _LOG_LEVELS:
        raise ConfigError(f"LOG_LEVEL은 {'/'.join(_LOG_LEVELS)} 중 하나여야 합니다: {log_level!r}")

    settings = Settings(
        discord_token=_required(env, "DISCORD_TOKEN"),
        discord_guild_id=_snowflake(env, "DISCORD_GUILD_ID"),
        ipo_channel_id=_snowflake(env, "IPO_CHANNEL_ID"),
        result_channel_id=_snowflake(env, "RESULT_CHANNEL_ID"),
        anthropic_api_key=_required(env, "ANTHROPIC_API_KEY"),
        dart_api_key=_required(env, "DART_API_KEY"),
        krx_auth_key=_required(env, "KRX_AUTH_KEY"),
        anthropic_summary_model=_optional(env, "ANTHROPIC_SUMMARY_MODEL", DEFAULT_SUMMARY_MODEL),
        anthropic_parse_model=_optional(env, "ANTHROPIC_PARSE_MODEL", DEFAULT_PARSE_MODEL),
        krx_base_url=_optional(env, "KRX_BASE_URL", "https://data-dbg.krx.co.kr/svc/apis").rstrip("/"),
        db_path=_optional(env, "DB_PATH", "./data/ipobot.sqlite3"),
        weekly_post_weekday=_weekday("WEEKLY_POST_WEEKDAY", _optional(env, "WEEKLY_POST_WEEKDAY", "FRI")),
        weekly_post_time=_time("WEEKLY_POST_TIME", _optional(env, "WEEKLY_POST_TIME", "18:00")),
        weekly_post_grace_hours=opt_int("WEEKLY_POST_GRACE_HOURS", 6, 1),
        weekly_retry_backoff_minutes=opt_int("WEEKLY_RETRY_BACKOFF_MINUTES", 15, 1),
        ipo_range_days=opt_int("IPO_RANGE_DAYS", 7, 1),
        base_lookback_trading_days=opt_int("BASE_LOOKBACK_TRADING_DAYS", 3, 1),
        judge_time=_time("JUDGE_TIME", _optional(env, "JUDGE_TIME", "09:00")),
        krx_poll_interval_minutes=opt_int("KRX_POLL_INTERVAL_MINUTES", 30, 1),
        krx_poll_deadline=_time("KRX_POLL_DEADLINE", _optional(env, "KRX_POLL_DEADLINE", "15:00")),
        judge_max_attempts=opt_int("JUDGE_MAX_ATTEMPTS", 5, 1),
        judge_stale_business_days=opt_int("JUDGE_STALE_BUSINESS_DAYS", 10, 1),
        summary_retry_days=opt_int("SUMMARY_RETRY_DAYS", 3, 1),
        scheduler_tick_seconds=opt_int("SCHEDULER_TICK_SECONDS", 60, 1),
        log_level=log_level,
    )

    if settings.judge_time >= settings.krx_poll_deadline:
        raise ConfigError("JUDGE_TIME은 KRX_POLL_DEADLINE보다 빨라야 합니다")
    if settings.weekly_retry_backoff_minutes >= settings.weekly_post_grace_hours * 60:
        raise ConfigError("WEEKLY_RETRY_BACKOFF_MINUTES는 WEEKLY_POST_GRACE_HOURS*60보다 작아야 합니다")
    return settings
