"""OpenDART 어댑터(S8). `services.ipo_digest.DartClient` 포트를 구현한다.

- `list.json`(pblntf_detail_ty=C001): status `000` 정상, `013` 조회 결과 없음(빈 목록), 그 밖은 DartUnavailable.
- `document.xml`: 원문 zip(XML 파일들) → 태그를 걷어낸 평문. 해당 원문 없음(`013`·`014`)은 None.
- `corpCode.xml`: zip 안 CORPCODE.xml → 종목코드가 있는 회사만. 파싱은 `asyncio.to_thread`로 한다.
- HTTP 오류·타임아웃·비200·JSON/zip 오류는 모두 DartUnavailable이다.
- estkRs.json은 상장예정일 필드가 없어(ADR 0001) 수집 경로에서 쓰지 않는다.
"""

from __future__ import annotations

import asyncio
import html
import io
import json
import logging
import re
import zipfile
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any
from xml.etree import ElementTree

import httpx

from ipobot.domain.models import DartFiling, Market, StockMasterEntry
from ipobot.domain.tickers import normalize_code
from ipobot.services.ipo_digest import DartUnavailable

log = logging.getLogger(__name__)

DART_BASE_URL = "https://opendart.fss.or.kr/api"
DEFAULT_TIMEOUT = httpx.Timeout(20.0)
LIST_PAGE_COUNT = 100
FILING_DETAIL_TYPE = "C001"  # 증권신고서(지분증권)
CORP_CLS_MARKET: dict[str, Market] = {"Y": "KOSPI", "K": "KOSDAQ", "N": "KONEX"}
_NO_DATA_STATUSES = frozenset({"013", "014"})
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_STATUS_XML_RE = re.compile(rb"<status>\s*(\d+)\s*</status>")
_MESSAGE_XML_RE = re.compile(rb"<message>(.*?)</message>", re.S)


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8", "cp949"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def xml_to_text(raw: bytes) -> str:
    """DART 원문 XML 한 파일 → 공백 정규화한 평문."""
    text = _TAG_RE.sub(" ", _decode(raw))
    return _SPACE_RE.sub(" ", html.unescape(text)).strip()


def document_zip_text(data: bytes) -> str:
    """document.xml zip → 파일 이름 순으로 이어 붙인 평문. 깨진 zip은 DartUnavailable."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = sorted(n for n in zf.namelist() if not n.endswith("/"))
            return " ".join(t for t in (xml_to_text(zf.read(n)) for n in names) if t)
    except zipfile.BadZipFile as exc:
        raise DartUnavailable(f"원문 zip 오류: {exc}") from exc


def parse_corp_code_zip(data: bytes) -> list[StockMasterEntry]:
    """corpCode.xml zip → 종목코드가 유효한 회사만. 같은 코드가 겹치면 최신 modify_date가 이긴다."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml_names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
            if not xml_names:
                raise DartUnavailable("corpCode zip에 XML이 없습니다")
            root = ElementTree.fromstring(zf.read(xml_names[0]))
    except zipfile.BadZipFile as exc:
        raise DartUnavailable(f"corpCode zip 오류: {exc}") from exc
    except ElementTree.ParseError as exc:
        raise DartUnavailable(f"corpCode XML 오류: {exc}") from exc
    best: dict[str, tuple[str, StockMasterEntry]] = {}
    skipped = 0
    for node in root.iter("list"):
        raw_code = (node.findtext("stock_code") or "").strip()
        if not raw_code:
            continue
        corp_code = (node.findtext("corp_code") or "").strip()
        corp_name = (node.findtext("corp_name") or "").strip()
        try:
            code = normalize_code(raw_code)
        except ValueError:
            skipped += 1
            continue
        if not corp_code or not corp_name:
            skipped += 1
            continue
        modified = (node.findtext("modify_date") or "").strip()
        cur = best.get(code)
        if cur is None or modified > cur[0]:
            best[code] = (modified, StockMasterEntry(stock_code=code, corp_code=corp_code, corp_name=corp_name))
    if skipped:
        log.warning("corp_code_rows_skipped count=%d", skipped)
    return [entry for _, entry in sorted(best.values(), key=lambda v: v[1].stock_code)]


def parse_filing(row: Mapping[str, Any]) -> DartFiling:
    """list.json 한 행. 필수 키 결측·날짜 형식 오류는 DartUnavailable(응답 구조 오류)."""
    try:
        rcept_no = str(row["rcept_no"]).strip()
        corp_code = str(row["corp_code"]).strip()
        corp_name = str(row["corp_name"]).strip()
        rcept_dt = datetime.strptime(str(row["rcept_dt"]), "%Y%m%d").date()
    except (KeyError, ValueError) as exc:
        raise DartUnavailable(f"list.json 행 형식 오류: {exc}") from exc
    if not rcept_no or not corp_code or not corp_name:
        raise DartUnavailable(f"list.json 행 필수값 결측: {dict(row)}")
    raw_code = str(row.get("stock_code") or "").strip()
    stock_code: str | None = None
    if raw_code:
        try:
            stock_code = normalize_code(raw_code)
        except ValueError:
            log.warning("dart_stock_code_invalid rcept_no=%s stock_code=%r", rcept_no, raw_code)
    return DartFiling(
        rcept_no=rcept_no,
        corp_code=corp_code,
        corp_name=corp_name,
        report_nm=str(row.get("report_nm") or "").strip(),
        rcept_dt=rcept_dt,
        market=CORP_CLS_MARKET.get(str(row.get("corp_cls") or "").strip()),
        stock_code=stock_code,
    )


def _error_status(content: bytes) -> tuple[str | None, str]:
    """zip이 아닌 응답(오류 JSON·XML)에서 status·message를 꺼낸다."""
    try:
        obj = json.loads(content)
        if isinstance(obj, dict):
            return (str(obj.get("status")) if obj.get("status") is not None else None, str(obj.get("message", "")))
    except ValueError:
        pass
    m = _STATUS_XML_RE.search(content)
    if m is None:
        return None, ""
    msg = _MESSAGE_XML_RE.search(content)
    return m.group(1).decode(), _decode(msg.group(1)).strip() if msg else ""


class HttpDartClient:
    """httpx.AsyncClient 기반 OpenDART 클라이언트. `client`를 넘기면 수명은 호출자가 관리한다."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DART_BASE_URL,
        client: httpx.AsyncClient | None = None,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> HttpDartClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _get(self, path: str, params: Mapping[str, str]) -> httpx.Response:
        try:
            resp = await self._client.get(
                f"{self._base_url}/{path}", params={"crtfc_key": self._api_key, **params}
            )
        except httpx.HTTPError as exc:
            raise DartUnavailable(f"{path} 요청 실패: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise DartUnavailable(f"{path} HTTP {resp.status_code}")
        return resp

    async def list_filings(self, bgn: date, end: date) -> list[DartFiling]:
        filings: list[DartFiling] = []
        page = 1
        while True:
            resp = await self._get(
                "list.json",
                {
                    "bgn_de": bgn.strftime("%Y%m%d"),
                    "end_de": end.strftime("%Y%m%d"),
                    "pblntf_detail_ty": FILING_DETAIL_TYPE,
                    "page_no": str(page),
                    "page_count": str(LIST_PAGE_COUNT),
                },
            )
            try:
                payload = resp.json()
            except ValueError as exc:
                raise DartUnavailable("list.json JSON 오류") from exc
            if not isinstance(payload, dict):
                raise DartUnavailable("list.json 응답이 객체가 아닙니다")
            status = str(payload.get("status"))
            if status == "013":
                return filings
            if status != "000":
                raise DartUnavailable(f"list.json status {status}: {payload.get('message', '')}")
            rows = payload.get("list")
            if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                raise DartUnavailable("list.json `list` 형식 오류")
            filings.extend(parse_filing(r) for r in rows)
            try:
                total_page = int(payload.get("total_page", 1))
            except (TypeError, ValueError) as exc:
                raise DartUnavailable("list.json total_page 형식 오류") from exc
            if page >= total_page:
                return filings
            page += 1

    async def document_text(self, rcept_no: str) -> str | None:
        resp = await self._get("document.xml", {"rcept_no": rcept_no})
        if resp.content.startswith(b"PK"):
            return await asyncio.to_thread(document_zip_text, resp.content)
        status, message = _error_status(resp.content)
        if status in _NO_DATA_STATUSES:
            log.warning("dart_document_missing rcept_no=%s status=%s", rcept_no, status)
            return None
        raise DartUnavailable(f"document.xml status {status}: {message}")

    async def corp_codes(self) -> list[StockMasterEntry]:
        resp = await self._get("corpCode.xml", {})
        if not resp.content.startswith(b"PK"):
            status, message = _error_status(resp.content)
            raise DartUnavailable(f"corpCode.xml status {status}: {message}")
        return await asyncio.to_thread(parse_corp_code_zip, resp.content)
