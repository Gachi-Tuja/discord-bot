"""S8: DART 어댑터 + 상장 예정 수집(respx + 합성 fixture). 실제 외부 호출 없음."""

from __future__ import annotations

import io
import threading
import zipfile
from datetime import date, timedelta

import httpx
import pytest
import respx

from ipobot.__main__ import build_parser, preview_digest, sync_master
from ipobot.adapters import dart as dart_adapter
from ipobot.adapters.dart import HttpDartClient, document_zip_text, parse_corp_code_zip
from ipobot.db.connection import open_database
from ipobot.db.repositories import IpoListingRepo, StockMasterRepo
from ipobot.domain.models import DartFiling, IpoListing, StockMasterEntry
from ipobot.services.ipo_digest import (
    DART_FILING_LOOKBACK_DAYS,
    DART_UNAVAILABLE_TEXT,
    DartUnavailable,
    IpoDigestService,
    extract_listing_date,
    latest_filings,
    sync_stock_master,
)

BASE = "https://dart.test/api"
POST = date(2026, 11, 12)
BIO_RCEPT = "20261110000123"
BIO_PROSPECTUS = "20261001000077"
ROBOT_RCEPT = "20261028000456"


def zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def document_zip(text: str) -> bytes:
    return zip_bytes({f"{BIO_RCEPT}.xml": f"<DOCUMENT><BODY><P>{text}</P></BODY></DOCUMENT>".encode()})


def corpcode_zip(rows: list[dict]) -> bytes:
    items = "".join(
        "<list>" + "".join(f"<{k}>{v}</{k}>" for k, v in row.items()) + "</list>" for row in rows
    )
    return zip_bytes({"CORPCODE.xml": f"<?xml version='1.0' encoding='UTF-8'?><result>{items}</result>".encode()})


@pytest.fixture
def conn():
    c = open_database(":memory:")
    yield c
    c.close()


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as http:
        yield HttpDartClient("dart-key", base_url=BASE, client=http)


# ---------------------------------------------------------------- 상장일 추출 (증권발행실적보고서)


def test_extract_listing_date_from_fixture(load_fixture):
    fx = load_fixture("dart_document_listing.json")
    assert extract_listing_date(fx["text_excerpt"]) == date.fromisoformat(fx["expected_listing_date"])


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("3. 상장일(매매개시일) 2026년 11월 18일 (예정)", date(2026, 11, 18)),
        ("상장일 ( 매매개시일 ) : 2026. 12. 1.", date(2026, 12, 1)),
        ("상장일(매매개시일) 2026-11-30", date(2026, 11, 30)),
        ("청약기일: 2026년 11월 16일 납입기일: 2026년 11월 19일", None),
        ("상장예정일: 2026년 11월 25일", None),
        ("신주상장일(매매개시일) 2026년 11월 20일", None),
        ("상장일(매매개시일): 2026년 2월 30일", None),
        ("", None),
    ],
)
def test_extract_listing_date_variants(text, expected):
    assert extract_listing_date(text) == expected


def test_extract_listing_date_ignores_rights_offering_report(load_fixture):
    assert extract_listing_date(load_fixture("dart_document_rights_offering.json")["text_excerpt"]) is None


def test_document_zip_text_strips_tags_and_entities():
    data = zip_bytes({"b.xml": b"<P>&lt;2&gt;</P>", "a.xml": "<P>상장예정일:</P>\n<P>2026년 11월 25일</P>".encode()})
    assert document_zip_text(data) == "상장예정일: 2026년 11월 25일 <2>"


# ---------------------------------------------------------------- list.json


@respx.mock
async def test_list_filings_request_and_parse(client, load_fixture):
    route = respx.get(f"{BASE}/list.json").mock(
        return_value=httpx.Response(200, json=load_fixture("dart_list_c001.json"))
    )
    filings = await client.list_filings(date(2026, 8, 19), POST)
    params = dict(route.calls.last.request.url.params)
    assert params == {
        "crtfc_key": "dart-key",
        "bgn_de": "20260819",
        "end_de": "20261112",
        "pblntf_detail_ty": "C001",
        "page_no": "1",
        "page_count": "100",
    }
    assert [f.rcept_no for f in filings] == [BIO_PROSPECTUS, BIO_RCEPT, ROBOT_RCEPT]
    bio = filings[1]
    assert (bio.corp_code, bio.corp_name, bio.report_nm, bio.rcept_dt) == (
        "01987654",
        "가치토자바이오",
        "증권발행실적보고서",
        date(2026, 11, 10),
    )
    assert bio.market is None and bio.stock_code is None  # corp_cls E, 종목코드 미부여


@respx.mock
async def test_list_filings_paginates(client, load_fixture):
    page = load_fixture("dart_list_c001.json")
    p1 = {**page, "total_page": 2, "list": page["list"][:1]}
    p2 = {**page, "page_no": 2, "total_page": 2, "list": [{**page["list"][2], "corp_cls": "K", "stock_code": "0158S0"}]}
    route = respx.get(f"{BASE}/list.json").mock(
        side_effect=[httpx.Response(200, json=p1), httpx.Response(200, json=p2)]
    )
    filings = await client.list_filings(date(2026, 8, 19), POST)
    assert [c.request.url.params["page_no"] for c in route.calls] == ["1", "2"]
    assert filings[1].market == "KOSDAQ" and filings[1].stock_code == "0158S0"


@respx.mock
async def test_list_filings_status_013_is_empty(client, load_fixture):
    respx.get(f"{BASE}/list.json").mock(return_value=httpx.Response(200, json=load_fixture("dart_list_empty.json")))
    assert await client.list_filings(date(2026, 8, 19), POST) == []


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(503, text="maintenance"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"status": "020", "message": "요청 제한을 초과하였습니다."}),
        httpx.Response(200, json={"status": "000", "total_page": 1, "list": [{"corp_code": "1"}]}),
    ],
)
@respx.mock
async def test_list_filings_failures_raise(client, response):
    respx.get(f"{BASE}/list.json").mock(return_value=response)
    with pytest.raises(DartUnavailable):
        await client.list_filings(date(2026, 8, 19), POST)


@respx.mock
async def test_list_filings_timeout_raises(client):
    respx.get(f"{BASE}/list.json").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(DartUnavailable):
        await client.list_filings(date(2026, 8, 19), POST)


# ---------------------------------------------------------------- document.xml


@respx.mock
async def test_document_text_from_zip(client, load_fixture):
    fx = load_fixture("dart_document_listing.json")
    route = respx.get(f"{BASE}/document.xml").mock(
        return_value=httpx.Response(200, content=document_zip(fx["text_excerpt"]))
    )
    text = await client.document_text(BIO_RCEPT)
    assert route.calls.last.request.url.params["rcept_no"] == BIO_RCEPT
    assert extract_listing_date(text) == date.fromisoformat(fx["expected_listing_date"])


@respx.mock
async def test_document_missing_is_none(client):
    respx.get(f"{BASE}/document.xml").mock(
        return_value=httpx.Response(200, json={"status": "014", "message": "파일이 존재하지 않습니다."})
    )
    assert await client.document_text(BIO_RCEPT) is None


@respx.mock
async def test_document_error_status_xml_raises(client):
    body = "<result><status>020</status><message>요청 제한 초과</message></result>".encode()
    respx.get(f"{BASE}/document.xml").mock(return_value=httpx.Response(200, content=body))
    with pytest.raises(DartUnavailable, match="020"):
        await client.document_text(BIO_RCEPT)


@respx.mock
async def test_document_5xx_raises(client):
    respx.get(f"{BASE}/document.xml").mock(return_value=httpx.Response(502))
    with pytest.raises(DartUnavailable):
        await client.document_text(BIO_RCEPT)


# ---------------------------------------------------------------- corpCode.xml


def test_parse_corp_code_zip_filters_and_dedupes(load_fixture):
    rows = load_fixture("dart_corpcode_sample.json")["list"] + [
        {"corp_code": "00000001", "corp_name": "옛이름", "stock_code": "005930", "modify_date": "20200101"},
        {"corp_code": "00000002", "corp_name": "잘못된코드", "stock_code": "12AB", "modify_date": "20261111"},
    ]
    entries = parse_corp_code_zip(corpcode_zip(rows))
    assert entries == [
        StockMasterEntry(stock_code="005930", corp_code="00126380", corp_name="삼성전자"),
        StockMasterEntry(stock_code="0158S0", corp_code="01987654", corp_name="가치토자바이오"),
    ]


def test_parse_corp_code_bad_zip_raises():
    with pytest.raises(DartUnavailable):
        parse_corp_code_zip(b"PKnot-a-zip")


@respx.mock
async def test_corp_codes_parses_in_worker_thread(client, load_fixture, monkeypatch):
    threads: list[int] = []
    original = dart_adapter.parse_corp_code_zip

    def spy(data: bytes):
        threads.append(threading.get_ident())
        return original(data)

    monkeypatch.setattr(dart_adapter, "parse_corp_code_zip", spy)
    respx.get(f"{BASE}/corpCode.xml").mock(
        return_value=httpx.Response(200, content=corpcode_zip(load_fixture("dart_corpcode_sample.json")["list"]))
    )
    entries = await client.corp_codes()
    assert len(entries) == 2
    assert threads and threads[0] != threading.get_ident()


@respx.mock
async def test_corp_codes_error_status_raises(client):
    respx.get(f"{BASE}/corpCode.xml").mock(
        return_value=httpx.Response(200, json={"status": "010", "message": "등록되지 않은 키입니다."})
    )
    with pytest.raises(DartUnavailable, match="010"):
        await client.corp_codes()


# ---------------------------------------------------------------- 수집 서비스 (fake 포트)


RESULT_REPORT = "증권발행실적보고서"
PROSPECTUS = "증권신고서(지분증권)"


def filing(rcept_no: str, corp_code: str, corp_name: str, report_nm: str = RESULT_REPORT, **kw) -> DartFiling:
    return DartFiling(
        rcept_no=rcept_no,
        corp_code=corp_code,
        corp_name=corp_name,
        report_nm=report_nm,
        rcept_dt=date(2026, 10, 20),
        **kw,
    )


class FakeDart:
    def __init__(self, filings=(), documents=None, corp=(), fail: Exception | None = None):
        self.filings = list(filings)
        self.documents = dict(documents or {})
        self.corp = list(corp)
        self.fail = fail
        self.list_calls: list[tuple[date, date]] = []
        self.document_calls: list[str] = []

    async def list_filings(self, bgn, end):
        self.list_calls.append((bgn, end))
        if self.fail:
            raise self.fail
        return list(self.filings)

    async def document_text(self, rcept_no):
        self.document_calls.append(rcept_no)
        doc = self.documents.get(rcept_no)
        if isinstance(doc, Exception):
            raise doc
        return doc

    async def corp_codes(self):
        if self.fail:
            raise self.fail
        return list(self.corp)


def doc(day: str) -> str:
    y, m, d = day.split("-")
    return f"증권발행실적보고서 ... 3. 상장일(매매개시일) {int(y)}년 {int(m)}월 {int(d)}일 (예정)"


async def test_build_digest_range_and_order(conn, fake_clock):
    client = FakeDart(
        filings=[
            filing("20261001000001", "C1", "나중회사", market="KOSDAQ"),
            filing("20261001000002", "C2", "가나다", market="KOSPI"),
            filing("20261001000003", "C3", "가가가"),
            filing("20261001000004", "C4", "당일상장"),
            filing("20261001000005", "C5", "범위밖"),
            filing("20261001000006", "C6", "경계"),
            filing("20261001000007", "C7", "추출실패"),
            filing("20261001000008", "C8", "원문없음"),
        ],
        documents={
            "20261001000001": doc("2026-11-20"),
            "20261001000002": doc("2026-11-13"),
            "20261001000003": doc("2026-11-13"),
            "20261001000004": doc("2026-11-12"),
            "20261001000005": doc("2026-11-27"),
            "20261001000006": doc("2026-11-26"),
            "20261001000007": "주권상장 및 유통 예정일: 2026년 11월 20일",
            "20261001000008": None,
        },
    )
    service = IpoDigestService(client, conn, fake_clock, range_days=14)
    listings = await service.build_digest(POST)
    assert [(it.corp_name, it.listing_date.isoformat()) for it in listings] == [
        ("가가가", "2026-11-13"),
        ("가나다", "2026-11-13"),
        ("나중회사", "2026-11-20"),
        ("경계", "2026-11-26"),
    ]
    assert client.list_calls == [(date(2026, 8, 19), POST)]
    assert POST - client.list_calls[0][0] == timedelta(days=DART_FILING_LOOKBACK_DAYS)
    assert listings[1].market == "KOSPI" and listings[0].market is None


async def test_build_digest_uses_latest_amendment_and_caches(conn, fake_clock):
    client = FakeDart(
        filings=[
            filing("20261001000001", "C1", "정정회사"),
            filing("20261015000009", "C1", "정정회사"),
        ],
        documents={"20261001000001": doc("2026-11-14"), "20261015000009": doc("2026-11-18")},
    )
    service = IpoDigestService(client, conn, fake_clock, range_days=14)
    first = await service.build_digest(POST)
    assert [(it.source_rcept_no, it.listing_date) for it in first] == [("20261015000009", date(2026, 11, 18))]
    assert client.document_calls == ["20261015000009"]
    second = await service.build_digest(POST)
    assert second == first
    assert client.document_calls == ["20261015000009"]  # 같은 접수번호는 원문을 다시 받지 않는다


async def test_build_digest_replaces_old_schedule(conn, fake_clock):
    repo = IpoListingRepo(conn)
    repo.upsert_many(
        [IpoListing("C1", "정정회사", date(2026, 11, 14), "20261001000001")], fake_clock.now()
    )
    client = FakeDart(filings=[filing("20261015000009", "C1", "정정회사")], documents={"20261015000009": doc("2026-11-18")})
    await IpoDigestService(client, conn, fake_clock, range_days=14).build_digest(POST)
    rows = conn.execute("SELECT listing_date, source_rcept_no FROM ipo_listings").fetchall()
    assert [tuple(r) for r in rows] == [("2026-11-18", "20261015000009")]


async def test_build_digest_fills_stock_code_from_master(conn, fake_clock):
    StockMasterRepo(conn).upsert_all(
        [StockMasterEntry(stock_code="0158S0", corp_code="C1", corp_name="가치토자바이오")], fake_clock.now()
    )
    client = FakeDart(filings=[filing("20261001000001", "C1", "가치토자바이오")], documents={"20261001000001": doc("2026-11-20")})
    [listing] = await IpoDigestService(client, conn, fake_clock, range_days=14).build_digest(POST)
    assert listing.stock_code == "0158S0"


@pytest.mark.parametrize("error", [DartUnavailable("list.json HTTP 503"), DartUnavailable("timeout")])
async def test_build_digest_list_failure_propagates(conn, fake_clock, error):
    client = FakeDart(fail=error)
    with pytest.raises(DartUnavailable):
        await IpoDigestService(client, conn, fake_clock, range_days=14).build_digest(POST)


async def test_build_digest_document_failure_propagates_and_saves_nothing(conn, fake_clock):
    client = FakeDart(
        filings=[filing("20261001000001", "C1", "A"), filing("20261001000002", "C2", "B")],
        documents={"20261001000001": doc("2026-11-20"), "20261001000002": DartUnavailable("HTTP 500")},
    )
    with pytest.raises(DartUnavailable):
        await IpoDigestService(client, conn, fake_clock, range_days=14).build_digest(POST)
    assert conn.execute("SELECT COUNT(*) FROM ipo_listings").fetchone()[0] == 0


@respx.mock
async def test_build_digest_end_to_end_5xx_propagates(conn, fake_clock, client):
    respx.get(f"{BASE}/list.json").mock(return_value=httpx.Response(500))
    with pytest.raises(DartUnavailable):
        await IpoDigestService(client, conn, fake_clock, range_days=14).build_digest(POST)


@respx.mock
async def test_build_digest_end_to_end_fixture(conn, fake_clock, client, load_fixture):
    respx.get(f"{BASE}/list.json").mock(return_value=httpx.Response(200, json=load_fixture("dart_list_c001.json")))
    fx = load_fixture("dart_document_listing.json")
    respx.get(f"{BASE}/document.xml", params={"rcept_no": BIO_RCEPT}).mock(
        return_value=httpx.Response(200, content=document_zip(fx["text_excerpt"]))
    )
    listings = await IpoDigestService(client, conn, fake_clock, range_days=7).build_digest(POST)
    assert [(it.corp_name, it.listing_date, it.market, it.source_rcept_no, it.summary_rcept_no) for it in listings] == [
        ("가치토자바이오", date(2026, 11, 18), None, BIO_RCEPT, BIO_PROSPECTUS)
    ]  # 실적보고서가 없는 샘플로보틱스는 원문을 받지 않고 빠진다


async def test_build_digest_only_reads_result_reports_and_links_summary_source(conn, fake_clock):
    client = FakeDart(
        filings=[
            filing("20261001000001", "C1", "공모회사", PROSPECTUS),
            filing("20261020000002", "C1", "공모회사", "[기재정정]증권신고서(지분증권)"),
            filing("20261021000003", "C1", "공모회사", "투자설명서"),
            filing("20261022000004", "C1", "공모회사", "[첨부정정]투자설명서"),
            filing("20261110000005", "C1", "공모회사"),
            filing("20261001000006", "C2", "신고서만", PROSPECTUS),
            filing("20261111000007", "C3", "실적만"),
            filing("20261112000008", "C4", "철회회사", "철회신고서"),
        ],
        documents={"20261110000005": doc("2026-11-18"), "20261111000007": doc("2026-11-19")},
    )
    listings = await IpoDigestService(client, conn, fake_clock, range_days=7).build_digest(POST)
    assert [(it.corp_name, it.source_rcept_no, it.summary_rcept_no) for it in listings] == [
        ("공모회사", "20261110000005", "20261021000003"),
        ("실적만", "20261111000007", None),
    ]
    assert sorted(client.document_calls) == ["20261110000005", "20261111000007"]
    assert IpoListingRepo(conn).by_rcept_no("20261110000005").summary_rcept_no == "20261021000003"


def test_latest_filings_picks_max_rcept_no():
    a = filing("20261001000001", "C1", "A")
    b = filing("20261015000009", "C1", "A")
    c = filing("20261002000001", "C2", "B")
    assert latest_filings([b, c, a]) == [c, b]


def test_service_rejects_bad_range(conn, fake_clock):
    with pytest.raises(ValueError):
        IpoDigestService(FakeDart(), conn, fake_clock, range_days=0)


# ---------------------------------------------------------------- sync-master


async def test_sync_stock_master_single_transaction_upsert(conn, fake_clock):
    StockMasterRepo(conn).upsert_all(
        [StockMasterEntry(stock_code="999990", corp_code="OLD", corp_name="유지되는회사")], fake_clock.now()
    )
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    client = FakeDart(
        corp=[
            StockMasterEntry(stock_code="005930", corp_code="00126380", corp_name="삼성전자"),
            StockMasterEntry(stock_code="0158S0", corp_code="01987654", corp_name="가치토자바이오"),
        ]
    )
    assert await sync_stock_master(client, conn, fake_clock) == 2
    conn.set_trace_callback(None)
    assert sum(1 for s in statements if s.startswith("BEGIN")) == 1
    assert sum(1 for s in statements if s == "COMMIT") == 1
    rows = conn.execute("SELECT stock_code, corp_name FROM stock_master ORDER BY stock_code").fetchall()
    assert [tuple(r) for r in rows] == [("005930", "삼성전자"), ("0158S0", "가치토자바이오"), ("999990", "유지되는회사")]


async def test_sync_stock_master_empty_or_failure_writes_nothing(conn, fake_clock):
    with pytest.raises(DartUnavailable):
        await sync_stock_master(FakeDart(corp=[]), conn, fake_clock)
    with pytest.raises(DartUnavailable):
        await sync_stock_master(FakeDart(fail=DartUnavailable("HTTP 500")), conn, fake_clock)
    assert conn.execute("SELECT COUNT(*) FROM stock_master").fetchone()[0] == 0


# ---------------------------------------------------------------- CLI


def test_cli_parses_dart_commands():
    parser = build_parser()
    assert parser.parse_args(["sync-master"]).command == "sync-master"
    args = parser.parse_args(["preview-digest", "--date", "2026-11-12"])
    assert (args.command, args.post_date) == ("preview-digest", POST)
    assert parser.parse_args(["preview-digest"]).post_date is None


async def test_preview_digest_prints_parts(conn, fake_clock):
    client = FakeDart(filings=[filing("20261001000001", "C1", "가치토자바이오", market="KOSDAQ")],
                      documents={"20261001000001": doc("2026-11-25")})
    out = io.StringIO()
    assert await preview_digest(client, conn, fake_clock, 14, None, out) == 0
    text = out.getvalue()
    assert "파트 1/1" in text
    assert "가치토자바이오 · 2026-11-25 · KOSDAQ" in text
    assert "요약 없음" in text


async def test_preview_digest_empty_success(conn, fake_clock):
    out = io.StringIO()
    assert await preview_digest(FakeDart(), conn, fake_clock, 14, POST, out) == 0
    assert "향후 1주간 상장 예정 종목이 없습니다" in out.getvalue()


async def test_preview_digest_dart_failure_never_prints_empty_notice(conn, fake_clock):
    out = io.StringIO()
    assert await preview_digest(FakeDart(fail=DartUnavailable("HTTP 503")), conn, fake_clock, 14, POST, out) == 1
    text = out.getvalue()
    assert DART_UNAVAILABLE_TEXT in text
    assert "상장 예정 종목이 없습니다" not in text


async def test_sync_master_cli_reports_count(conn, fake_clock):
    out = io.StringIO()
    client = FakeDart(corp=[StockMasterEntry(stock_code="005930", corp_code="00126380", corp_name="삼성전자")])
    assert await sync_master(client, conn, fake_clock, out) == 0
    assert "1개" in out.getvalue()
    out = io.StringIO()
    assert await sync_master(FakeDart(fail=DartUnavailable("x")), conn, fake_clock, out) == 1
    assert DART_UNAVAILABLE_TEXT in out.getvalue()


def test_main_requires_dart_key(monkeypatch, capsys):
    from ipobot.__main__ import main

    monkeypatch.delenv("DART_API_KEY", raising=False)
    assert main(["sync-master"]) == 2
    assert "DART_API_KEY" in capsys.readouterr().err
