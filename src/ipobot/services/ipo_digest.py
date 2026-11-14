"""DART 상장 예정 종목 수집(S8, R15·R16·R18).

- `build_digest(post_date)`: list.json(C001) 중 증권발행실적보고서만 → 회사별 최신 접수번호 → document.xml
  원문에서 `상장일(매매개시일)` 추출 → `post_date+1 ≤ listing_date ≤ post_date+IPO_RANGE_DAYS`, 상장일·이름 순.
  증권신고서·투자설명서에는 상장일이 없고, 상장일은 상장 약 1주 전에 내는 증권발행실적보고서에만 확정 기재된다.
  유상증자 실적보고서는 `주권상장 및 유통 예정일`·`신주상장 예정일` 라벨을 써서 추출 대상에서 빠진다.
- 요약 원문은 같은 회사의 최신 증권신고서·투자설명서(`summary_rcept_no`)다. 없으면 실적보고서를 쓴다.
- 원문 추출 결과는 `ipo_listings`에 접수번호 단위로 캐시한다(같은 접수번호는 원문을 다시 받지 않는다).
- DART 장애는 `DartUnavailable`로 전파한다. 빈 목록으로 삼키지 않는다(0건 오게시 방지).
- `sync_stock_master`: corpCode.xml 파싱은 어댑터가 `asyncio.to_thread`로 하고, 저장은 트랜잭션 1회다.

`DartClient`는 이 모듈이 요구하는 포트다. `adapters/dart.py`가 구현한다.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, timedelta
from typing import Any, Protocol

from ipobot.clock import Clock
from ipobot.db.repositories import IpoListingRepo, StockMasterRepo
from ipobot.domain.models import DartFiling, IpoListing, StockMasterEntry

log = logging.getLogger(__name__)

# list.json은 corp_code 없이 검색할 때 검색 기간이 3개월로 제한된다. 여유를 두고 85일을 본다.
DART_FILING_LOOKBACK_DAYS = 85
DART_UNAVAILABLE_TEXT = "DART 조회에 실패했습니다. 잠시 후 다시 시도해 주세요"

RESULT_REPORT_MARK = "증권발행실적보고서"
# 요약 원문 후보에서 뺄 보고서: 철회신고서, 본문 없는 첨부정정, 실적보고서.
_SUMMARY_SKIP_MARKS = ("철회", "첨부정정", RESULT_REPORT_MARK)

_LISTING_DATE_RE = re.compile(
    r"(?<![가-힣])상\s*장\s*일\s*\(\s*매\s*매\s*개\s*시\s*일\s*\)\s*[:：]?\s*(\d{4})\s*(?:년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일|[.\-/]\s*(\d{1,2})\s*[.\-/]\s*(\d{1,2}))"
)


class DartUnavailable(Exception):
    """DART 조회 실패(HTTP 오류·타임아웃·오류 status·응답 구조 오류)."""


class DartClient(Protocol):
    async def list_filings(self, bgn: date, end: date) -> list[DartFiling]:
        """기간 내 증권신고서(지분증권) 목록. 조회 결과 없음은 빈 목록, 실패는 DartUnavailable."""
        ...

    async def document_text(self, rcept_no: str) -> str | None:
        """원문 평문. 해당 원문이 없으면 None, 실패는 DartUnavailable."""
        ...

    async def corp_codes(self) -> list[StockMasterEntry]:
        """corpCode.xml의 상장 종목. 실패는 DartUnavailable."""
        ...


def extract_listing_date(text: str) -> date | None:
    """원문에서 `상장일(매매개시일)` 라벨 뒤 첫 날짜. 없거나 달력에 없는 날짜면 None."""
    m = _LISTING_DATE_RE.search(text)
    if m is None:
        return None
    year = int(m.group(1))
    month = int(m.group(2) or m.group(4))
    day = int(m.group(3) or m.group(5))
    try:
        return date(year, month, day)
    except ValueError:
        return None


def is_result_report(filing: DartFiling) -> bool:
    return RESULT_REPORT_MARK in filing.report_nm


def is_summary_source(filing: DartFiling) -> bool:
    return not any(mark in filing.report_nm for mark in _SUMMARY_SKIP_MARKS)


def latest_filings(filings: Iterable[DartFiling]) -> list[DartFiling]:
    """회사별로 접수번호가 가장 큰(최신, 정정 포함) 공시만 남긴다."""
    latest: dict[str, DartFiling] = {}
    for f in filings:
        cur = latest.get(f.corp_code)
        if cur is None or f.rcept_no > cur.rcept_no:
            latest[f.corp_code] = f
    return sorted(latest.values(), key=lambda f: f.rcept_no)


def digest_items(
    listings: Sequence[IpoListing], summaries: Mapping[str, str | None] | None = None
) -> list[dict[str, Any]]:
    """`formatting.digest_parts` 입력. 요약은 접수번호로 찾고, 없으면 None(→ `요약 없음`)."""
    summaries = summaries or {}
    return [
        {
            "corp_name": it.corp_name,
            "listing_date": it.listing_date,
            "market": it.market,
            "summary": summaries.get(it.source_rcept_no),
        }
        for it in listings
    ]


class IpoDigestService:
    def __init__(self, client: DartClient, conn: sqlite3.Connection, clock: Clock, range_days: int) -> None:
        if range_days < 1:
            raise ValueError("range_days는 1 이상이어야 합니다")
        self._client = client
        self._conn = conn
        self._clock = clock
        self._range_days = range_days
        self._listings = IpoListingRepo(conn)
        self._master = StockMasterRepo(conn)

    async def build_digest(self, post_date: date) -> list[IpoListing]:
        """게시일 기준 상장 예정 종목. 수집 실패는 DartUnavailable로 전파한다."""
        filings = await self._client.list_filings(post_date - timedelta(days=DART_FILING_LOOKBACK_DAYS), post_date)
        resolved: list[IpoListing] = []
        fresh: list[IpoListing] = []
        summary_sources = {f.corp_code: f.rcept_no for f in latest_filings(filter(is_summary_source, filings))}
        for filing in latest_filings(filter(is_result_report, filings)):
            cached = self._listings.by_rcept_no(filing.rcept_no)
            if cached is not None:
                resolved.append(cached)
                continue
            listing = await self._resolve(filing, summary_sources.get(filing.corp_code))
            if listing is not None:
                resolved.append(listing)
                fresh.append(listing)
        if fresh:
            self._listings.upsert_many(fresh, self._clock.now())
        first, last = post_date + timedelta(days=1), post_date + timedelta(days=self._range_days)
        in_range = [it for it in resolved if first <= it.listing_date <= last]
        return sorted(in_range, key=lambda it: (it.listing_date, it.corp_name))

    async def _resolve(self, filing: DartFiling, summary_rcept_no: str | None) -> IpoListing | None:
        text = await self._client.document_text(filing.rcept_no)
        listing_date = extract_listing_date(text) if text else None
        if listing_date is None:
            # 유상증자 등 신규상장이 아닌 실적보고서는 라벨이 없어 여기로 온다(정상).
            log.info(
                "listing_date_not_found rcept_no=%s corp=%s", filing.rcept_no, filing.corp_name
            )
            return None
        stock_code = filing.stock_code or self._master.stock_code_for_corp(filing.corp_code)
        return IpoListing(
            corp_code=filing.corp_code,
            corp_name=filing.corp_name,
            listing_date=listing_date,
            source_rcept_no=filing.rcept_no,
            stock_code=stock_code,
            market=filing.market,
            summary_rcept_no=summary_rcept_no,
        )


async def sync_stock_master(client: DartClient, conn: sqlite3.Connection, clock: Clock) -> int:
    """corpCode.xml로 stock_master를 갱신한다. 빈 결과는 장애로 본다(마스터를 비우지 않는다)."""
    entries = await client.corp_codes()
    if not entries:
        raise DartUnavailable("corpCode.xml에 상장 종목이 없습니다")
    return StockMasterRepo(conn).upsert_all(entries, clock.now())
