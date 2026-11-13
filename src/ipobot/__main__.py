"""CLI 진입점: `python -m ipobot <command>`.

- `krx-probe YYYY-MM-DD [--code C]`: 3개 시장 KRX 응답을 조회해 행 수·이상 행·종목 관측을 출력한다.
  키·형식 확인용(CP-KRX)이며 DB에 쓰지 않는다(메모리 DB 사용).
- `sync-master`: DART corpCode.xml로 `stock_master`를 갱신한다(DB_PATH).
- `preview-digest [--date YYYY-MM-DD]`: 상장 예정 다이제스트를 게시하지 않고 출력한다(기본 오늘, KST).
- `run`: 디스코드 봇을 실행한다(전체 환경변수 필요).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from collections.abc import Sequence
from datetime import date
from typing import TextIO

from ipobot.bot.formatting import Part, digest_parts
from ipobot.clock import Clock, SystemClock, today_kst
from ipobot.config import ConfigError, _int, _optional
from ipobot.db.connection import open_database
from ipobot.domain.models import MARKETS, MarketDayRows
from ipobot.domain.tickers import normalize_code
from ipobot.services.ipo_digest import (
    DART_UNAVAILABLE_TEXT,
    DartClient,
    DartUnavailable,
    IpoDigestService,
    digest_items,
    sync_stock_master,
)
from ipobot.services.quotes import KrxAuthError, KrxClient, KrxFetchError, QuoteService

DEFAULT_KRX_BASE_URL = "https://data-dbg.krx.co.kr/svc/apis"


def _parse_date(raw: str) -> date:
    try:
        if len(raw) != 10:
            raise ValueError(raw)
        return date.fromisoformat(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"YYYY-MM-DD 형식이어야 합니다: {raw!r}") from None


def _parse_code(raw: str) -> str:
    try:
        return normalize_code(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ipobot")
    sub = parser.add_subparsers(dest="command", required=True)
    probe = sub.add_parser("krx-probe", help="KRX 일별매매정보 3개 시장 조회")
    probe.add_argument("trade_date", type=_parse_date, metavar="YYYY-MM-DD")
    probe.add_argument("--code", type=_parse_code, default=None)
    sub.add_parser("sync-master", help="DART corpCode.xml로 종목 마스터 갱신")
    preview = sub.add_parser("preview-digest", help="상장 예정 다이제스트 미리보기(게시 안 함)")
    preview.add_argument("--date", dest="post_date", type=_parse_date, default=None, metavar="YYYY-MM-DD")
    sub.add_parser("run", help="디스코드 봇 실행")
    return parser


async def krx_probe(client: KrxClient, trade_date: date, code: str | None, out: TextIO) -> int:
    """시장별 응답 요약을 출력한다. 하나라도 조회에 실패하면 1을 반환한다."""
    failed = False
    for market in MARKETS:
        try:
            rows: MarketDayRows = await client.fetch_market_day(market, trade_date)
        except KrxAuthError as exc:
            print(f"{market}: 인증 오류 ({exc})", file=out)
            failed = True
            continue
        except KrxFetchError as exc:
            print(f"{market}: 조회 실패 ({exc})", file=out)
            failed = True
            continue
        state = "빈 응답" if rows.is_empty else "공개"
        print(f"{market}: {state} 행 {len(rows.quotes)}개, 이상 행 {len(rows.anomalies)}개", file=out)
        for anomaly in rows.anomalies:
            print(f"  이상 행 code={anomaly.stock_code} 사유={anomaly.reason}", file=out)
    if code is not None:
        conn = open_database(":memory:")
        try:
            obs = await QuoteService(client, conn, SystemClock()).day_obs(code, trade_date, None)
        finally:
            conn.close()
        print(f"{code}: {obs}", file=out)
    return 1 if failed else 0


def part_text(part: Part) -> str:
    """미리보기용 평문 렌더링."""
    lines: list[str] = []
    if part.content:
        lines.append(part.content)
    for embed in part.embeds:
        if embed.get("title"):
            lines.append(f"# {embed['title']}")
        if embed.get("description"):
            lines.append(embed["description"])
        for field in embed.get("fields", []):
            lines.append(f"- {field['name']}: {field['value']}")
    return "\n".join(lines)


async def preview_digest(
    client: DartClient, conn: sqlite3.Connection, clock: Clock, range_days: int, post_date: date | None, out: TextIO
) -> int:
    """다이제스트를 파트 단위로 출력한다. DART 장애면 실패 안내만 출력하고 1을 반환한다(0건 안내 금지)."""
    day = post_date or today_kst(clock)
    try:
        listings = await IpoDigestService(client, conn, clock, range_days).build_digest(day)
    except DartUnavailable as exc:
        print(f"{DART_UNAVAILABLE_TEXT} ({exc})", file=out)
        return 1
    parts = digest_parts(digest_items(listings), day)
    for i, part in enumerate(parts):
        print(f"--- 파트 {i + 1}/{len(parts)} ---", file=out)
        print(part_text(part), file=out)
    return 0


async def sync_master(client: DartClient, conn: sqlite3.Connection, clock: Clock, out: TextIO) -> int:
    try:
        count = await sync_stock_master(client, conn, clock)
    except DartUnavailable as exc:
        print(f"{DART_UNAVAILABLE_TEXT} ({exc})", file=out)
        return 1
    print(f"stock_master {count}개 종목 갱신", file=out)
    return 0


def _dart_env() -> tuple[str, str, int]:
    key = os.environ.get("DART_API_KEY", "").strip()
    if not key:
        raise ConfigError("필수 환경변수 DART_API_KEY가 없습니다")
    db_path = _optional(os.environ, "DB_PATH", "./data/ipobot.sqlite3")
    range_days = _int("IPO_RANGE_DAYS", _optional(os.environ, "IPO_RANGE_DAYS", "14"), minimum=1)
    return key, db_path, range_days


async def _run_dart_command(args: argparse.Namespace) -> int:
    from ipobot.adapters.dart import HttpDartClient

    key, db_path, range_days = _dart_env()
    if db_path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = open_database(db_path)
    try:
        async with HttpDartClient(key) as client:
            if args.command == "sync-master":
                return await sync_master(client, conn, SystemClock(), sys.stdout)
            return await preview_digest(client, conn, SystemClock(), range_days, args.post_date, sys.stdout)
    finally:
        conn.close()


def _krx_env() -> tuple[str, str]:
    key = os.environ.get("KRX_AUTH_KEY", "").strip()
    if not key:
        raise ConfigError("필수 환경변수 KRX_AUTH_KEY가 없습니다")
    base = os.environ.get("KRX_BASE_URL", "").strip() or DEFAULT_KRX_BASE_URL
    return base.rstrip("/"), key


async def _run_krx_probe(args: argparse.Namespace) -> int:
    from ipobot.adapters.krx import HttpKrxClient

    base_url, key = _krx_env()
    async with HttpKrxClient(base_url, key) as client:
        return await krx_probe(client, args.trade_date, args.code, sys.stdout)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "krx-probe":
            return asyncio.run(_run_krx_probe(args))
        if args.command in ("sync-master", "preview-digest"):
            return asyncio.run(_run_dart_command(args))
        if args.command == "run":
            from ipobot.bot.app import run

            return run()
    except ConfigError as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 2
    raise AssertionError(f"알 수 없는 명령: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
