"""S7: KRX 어댑터(respx + 합성 fixture). 실제 외부 호출 없음."""

from __future__ import annotations

import io
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from ipobot.__main__ import build_parser, krx_probe
from ipobot.adapters.krx import ENDPOINTS, HttpKrxClient, parse_number
from ipobot.domain.models import DailyQuote, NotTraded, RowAnomaly, Traded
from ipobot.services.quotes import KrxAuthError, KrxFetchError, QuoteService

BASE = "https://krx.test/svc/apis"
D = date(2026, 11, 10)
FIXTURE_BY_MARKET = {
    "KOSPI": "krx_kospi_sample.json",
    "KOSDAQ": "krx_kosdaq_alnum.json",
    "KONEX": "krx_konex_sample.json",
}


def url(market):
    return f"{BASE}/{ENDPOINTS[market]}"


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as http:
        yield HttpKrxClient(BASE, "secret-key", client=http)


@respx.mock
async def test_request_format_all_endpoints(client, load_fixture):
    routes = {
        m: respx.get(url(m)).mock(return_value=httpx.Response(200, json=load_fixture(f)))
        for m, f in FIXTURE_BY_MARKET.items()
    }
    assert {m: ENDPOINTS[m] for m in ENDPOINTS} == {
        "KOSPI": "sto/stk_bydd_trd",
        "KOSDAQ": "sto/ksq_bydd_trd",
        "KONEX": "sto/knx_bydd_trd",
    }
    for market, route in routes.items():
        await client.fetch_market_day(market, D)
        request = route.calls.last.request
        assert request.headers["AUTH_KEY"] == "secret-key"
        assert request.url.params["basDd"] == "20261110"
        assert dict(request.url.params) == {"basDd": "20261110"}


@respx.mock
async def test_kospi_comma_numbers_mapped(client, load_fixture):
    respx.get(url("KOSPI")).mock(return_value=httpx.Response(200, json=load_fixture("krx_kospi_sample.json")))
    rows = await client.fetch_market_day("KOSPI", D)
    assert rows.anomalies == ()
    assert rows.quotes[0] == DailyQuote(
        trade_date=D,
        market="KOSPI",
        stock_code="005930",
        corp_name="삼성전자",
        close_price=61_200,
        open_price=60_800,
        trade_volume=12_345_678,
        market_cap=365_346_123_456_000,
        listed_shares=5_969_782_550,
    )
    assert len(rows.quotes) == 3


@respx.mock
async def test_alphanumeric_code(client, load_fixture):
    respx.get(url("KOSDAQ")).mock(return_value=httpx.Response(200, json=load_fixture("krx_kosdaq_alnum.json")))
    rows = await client.fetch_market_day("KOSDAQ", D)
    codes = [quote.stock_code for quote in rows.quotes]
    assert "0158S0" in codes
    alnum = rows.quotes[codes.index("0158S0")]
    assert (alnum.close_price, alnum.market_cap, alnum.listed_shares) == (24_350, 487_000_000_000, 20_000_000)


@respx.mock
async def test_empty_response(client, load_fixture):
    respx.get(url("KONEX")).mock(return_value=httpx.Response(200, json=load_fixture("krx_empty.json")))
    rows = await client.fetch_market_day("KONEX", D)
    assert rows.is_empty


@respx.mock
async def test_halted_row_volume_zero(client, load_fixture):
    respx.get(url("KOSDAQ")).mock(return_value=httpx.Response(200, json=load_fixture("krx_halted.json")))
    rows = await client.fetch_market_day("KOSDAQ", D)
    halted = next(quote for quote in rows.quotes if quote.stock_code == "123450")
    assert (halted.trade_volume, halted.open_price, halted.close_price) == (0, 0, 5_670)


@respx.mock
async def test_anomaly_rows_marked_normal_row_parsed(client, load_fixture):
    respx.get(url("KOSDAQ")).mock(return_value=httpx.Response(200, json=load_fixture("krx_anomaly.json")))
    rows = await client.fetch_market_day("KOSDAQ", D)
    # MKTCAP="-" → 0 (QuoteService가 vol>0·close>0·mktcap≤0을 data_anomaly로 분류)
    dash = next(quote for quote in rows.quotes if quote.stock_code == "111110")
    assert dash.market_cap == 0
    assert rows.anomalies == (
        RowAnomaly("KOSDAQ", "222220", "MKTCAP 결측", trade_volume=45_678, close_price=9_900),
    )
    normal = next(quote for quote in rows.quotes if quote.stock_code == "247540")
    assert normal.market_cap == 11_912_345_678_400


@pytest.mark.parametrize("status", [401, 403])
@respx.mock
async def test_auth_errors(client, status):
    respx.get(url("KOSPI")).mock(return_value=httpx.Response(status, text="Unauthorized"))
    with pytest.raises(KrxAuthError):
        await client.fetch_market_day("KOSPI", D)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(503, text="busy"),
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json={"respMsg": "no block"}),
        httpx.Response(200, json={"OutBlock_1": "x"}),
        httpx.Response(200, json={"OutBlock_1": [{"BAS_DD": "20261109", "ISU_CD": "005930"}]}),
    ],
)
@respx.mock
async def test_transient_errors(client, response):
    respx.get(url("KOSPI")).mock(return_value=response)
    with pytest.raises(KrxFetchError):
        await client.fetch_market_day("KOSPI", D)


@respx.mock
async def test_timeout_is_fetch_error(client):
    respx.get(url("KOSPI")).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(KrxFetchError):
        await client.fetch_market_day("KOSPI", D)


def test_row_level_parsing_rules():
    assert parse_number("1,234,567") == 1_234_567
    assert parse_number("") == 0
    assert parse_number("-") == 0
    assert parse_number("0") == 0
    for bad in ("12,34", "1.5", "abc", "-5", None, True):
        with pytest.raises(ValueError):
            parse_number(bad)


@respx.mock
async def test_bad_code_and_non_numeric_become_row_anomalies(client):
    payload = {
        "OutBlock_1": [
            {"BAS_DD": "20261110", "ISU_CD": "??", "TDD_CLSPRC": "1", "ACC_TRDVOL": "1", "MKTCAP": "1", "LIST_SHRS": "1"},
            {"BAS_DD": "20261110", "ISU_CD": "A005930", "TDD_CLSPRC": "N/A", "ACC_TRDVOL": "0", "MKTCAP": "1", "LIST_SHRS": "1"},
        ]
    }
    respx.get(url("KOSPI")).mock(return_value=httpx.Response(200, json=payload))
    rows = await client.fetch_market_day("KOSPI", D)
    assert rows.quotes == ()
    assert rows.anomalies[0].stock_code is None
    assert rows.anomalies[1].stock_code == "005930"
    assert (rows.anomalies[1].trade_volume, rows.anomalies[1].close_price) == (0, None)


@respx.mock
async def test_adapter_with_quote_service(client, load_fixture, tmp_path, fake_clock):
    from ipobot.db.connection import open_database

    for market, name in {**FIXTURE_BY_MARKET, "KOSDAQ": "krx_halted.json"}.items():
        respx.get(url(market)).mock(return_value=httpx.Response(200, json=load_fixture(name)))
    conn = open_database(tmp_path / "db.sqlite3")
    try:
        service = QuoteService(client, conn, fake_clock)
        assert await service.day_obs("123450", D, "KOSDAQ") == NotTraded("halted")
        assert await service.day_obs("343090", D, "KONEX") == NotTraded("halted")
        traded = await service.day_obs("005930", D, None)
        assert isinstance(traded, Traded) and traded.quote.close_price == 61_200
        assert await service.day_obs("999999", D, "KOSPI") == NotTraded("absent")
    finally:
        conn.close()


@respx.mock
async def test_krx_probe_output(client, load_fixture):
    respx.get(url("KOSPI")).mock(return_value=httpx.Response(200, json=load_fixture("krx_kospi_sample.json")))
    respx.get(url("KOSDAQ")).mock(return_value=httpx.Response(200, json=load_fixture("krx_anomaly.json")))
    respx.get(url("KONEX")).mock(return_value=httpx.Response(403))
    out = io.StringIO()
    code = await krx_probe(client, D, "005930", out)
    text = out.getvalue()
    assert code == 1
    assert "KOSPI: 공개 행 3개, 이상 행 0개" in text
    assert "KOSDAQ: 공개 행 2개, 이상 행 1개" in text
    assert "222220" in text
    assert "KONEX: 인증 오류" in text
    assert "005930:" in text


def test_krx_probe_cli_args():
    args = build_parser().parse_args(["krx-probe", "2026-11-10", "--code", "A0158S0"])
    assert (args.trade_date, args.code) == (D, "0158S0")
    with pytest.raises(SystemExit):
        build_parser().parse_args(["krx-probe", "20261110"])


def test_no_kis_traces():
    root = Path(__file__).resolve().parents[2] / "src"
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "KIS" not in text and "kis_" not in text.lower(), path
