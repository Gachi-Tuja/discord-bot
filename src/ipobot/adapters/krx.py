"""KRX OpenAPI 일별매매정보 어댑터(S7). `services.quotes.KrxClient` 포트를 구현한다.

- 요청: `GET {base}/sto/{stk|ksq|knx}_bydd_trd?basDd=YYYYMMDD`, 헤더 `AUTH_KEY`.
- 401·403 → KrxAuthError. 그 밖의 HTTP 오류·타임아웃·JSON 오류·`OutBlock_1` 결측 → KrxFetchError(응답 전체).
- 행 단위: 콤마 숫자 → int, `""`·`-` → 0. 필수 필드(TDD_CLSPRC·ACC_TRDVOL·MKTCAP·LIST_SHRS·ISU_CD)가
  결측·비숫자면 그 행만 RowAnomaly로 표시한다(R9).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from typing import Any

import httpx

from ipobot.domain.models import DailyQuote, Market, MarketDayRows, RowAnomaly
from ipobot.domain.tickers import normalize_code
from ipobot.services.quotes import KrxAuthError, KrxFetchError

ENDPOINTS: dict[Market, str] = {
    "KOSPI": "sto/stk_bydd_trd",
    "KOSDAQ": "sto/ksq_bydd_trd",
    "KONEX": "sto/knx_bydd_trd",
}
DEFAULT_TIMEOUT = httpx.Timeout(15.0)
_NUMBER_RE = re.compile(r"^\d{1,3}(,\d{3})*$|^\d+$")


class _Bad(ValueError):
    """필수 필드 결측·비숫자."""


def parse_number(raw: object) -> int:
    """KRX 숫자 문자열 → int. `""`·`-` → 0, 콤마 허용. 그 밖은 _Bad."""
    if isinstance(raw, bool) or raw is None:
        raise _Bad(f"숫자가 아닙니다: {raw!r}")
    if isinstance(raw, int):
        if raw < 0:
            raise _Bad(f"음수: {raw!r}")
        return raw
    if not isinstance(raw, str):
        raise _Bad(f"숫자가 아닙니다: {raw!r}")
    text = raw.strip()
    if text in ("", "-"):
        return 0
    if not _NUMBER_RE.match(text):
        raise _Bad(f"숫자가 아닙니다: {raw!r}")
    return int(text.replace(",", ""))


def _field(row: Mapping[str, Any], key: str) -> int:
    if key not in row:
        raise _Bad(f"{key} 결측")
    try:
        return parse_number(row[key])
    except _Bad as exc:
        raise _Bad(f"{key}: {exc}") from None


def _maybe(row: Mapping[str, Any], key: str) -> int | None:
    try:
        return _field(row, key)
    except _Bad:
        return None


def parse_row(row: Mapping[str, Any], market: Market, trade_date: date) -> DailyQuote | RowAnomaly:
    """응답 한 행을 DailyQuote 또는 RowAnomaly로 바꾼다."""
    code: str | None
    try:
        code = normalize_code(row["ISU_CD"]) if "ISU_CD" in row else None
    except ValueError:
        code = None
    try:
        if code is None:
            raise _Bad("ISU_CD 결측·형식 오류")
        close = _field(row, "TDD_CLSPRC")
        volume = _field(row, "ACC_TRDVOL")
        mktcap = _field(row, "MKTCAP")
        shares = _field(row, "LIST_SHRS")
    except _Bad as exc:
        return RowAnomaly(
            market,
            code,
            str(exc),
            trade_volume=_maybe(row, "ACC_TRDVOL"),
            close_price=_maybe(row, "TDD_CLSPRC"),
        )
    open_price = _maybe(row, "TDD_OPNPRC") or 0
    name = row.get("ISU_NM")
    return DailyQuote(
        trade_date=trade_date,
        market=market,
        stock_code=code,
        corp_name=name.strip() if isinstance(name, str) else "",
        close_price=close,
        open_price=open_price,
        trade_volume=volume,
        market_cap=mktcap,
        listed_shares=shares,
    )


def parse_market_day(payload: object, market: Market, trade_date: date) -> MarketDayRows:
    """응답 JSON 전체 → MarketDayRows. 구조 오류·다른 일자 행은 KrxFetchError."""
    if not isinstance(payload, dict) or not isinstance(payload.get("OutBlock_1"), list):
        raise KrxFetchError(f"KRX 응답 구조 오류(OutBlock_1 없음) market={market}")
    expected = trade_date.strftime("%Y%m%d")
    quotes: list[DailyQuote] = []
    anomalies: list[RowAnomaly] = []
    for row in payload["OutBlock_1"]:
        if not isinstance(row, dict):
            raise KrxFetchError(f"KRX 응답 구조 오류(행이 객체가 아님) market={market}")
        bas_dd = row.get("BAS_DD")
        if bas_dd is not None and str(bas_dd).strip() != expected:
            raise KrxFetchError(f"KRX 응답 일자 불일치: 요청 {expected}, 응답 {bas_dd!r} market={market}")
        parsed = parse_row(row, market, trade_date)
        if isinstance(parsed, DailyQuote):
            quotes.append(parsed)
        else:
            anomalies.append(parsed)
    return MarketDayRows(trade_date, market, tuple(quotes), tuple(anomalies))


class HttpKrxClient:
    """httpx.AsyncClient 기반 KRX 클라이언트. `client`를 넘기면 수명은 호출자가 관리한다."""

    def __init__(
        self,
        base_url: str,
        auth_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth_key = auth_key
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> HttpKrxClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def fetch_market_day(self, market: Market, trade_date: date) -> MarketDayRows:
        url = f"{self._base_url}/{ENDPOINTS[market]}"
        try:
            response = await self._client.get(
                url,
                params={"basDd": trade_date.strftime("%Y%m%d")},
                headers={"AUTH_KEY": self._auth_key},
            )
        except httpx.HTTPError as exc:
            raise KrxFetchError(f"KRX 요청 실패 market={market}: {type(exc).__name__}") from exc
        if response.status_code in (401, 403):
            raise KrxAuthError(f"KRX 인증 오류 HTTP {response.status_code} market={market}")
        if response.status_code != 200:
            raise KrxFetchError(f"KRX HTTP {response.status_code} market={market}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise KrxFetchError(f"KRX JSON 오류 market={market}") from exc
        return parse_market_day(payload, market, trade_date)
