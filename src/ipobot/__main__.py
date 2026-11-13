"""CLI 진입점: `python -m ipobot <command>`.

- `krx-probe YYYY-MM-DD [--code C]`: 3개 시장 KRX 응답을 조회해 행 수·이상 행·종목 관측을 출력한다.
  키·형식 확인용(CP-KRX)이며 DB에 쓰지 않는다(메모리 DB 사용).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from datetime import date
from typing import TextIO

from ipobot.clock import SystemClock
from ipobot.config import ConfigError
from ipobot.db.connection import open_database
from ipobot.domain.models import MARKETS, MarketDayRows
from ipobot.domain.tickers import normalize_code
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
    except ConfigError as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 2
    raise AssertionError(f"알 수 없는 명령: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
