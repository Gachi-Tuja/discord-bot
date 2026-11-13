"""DART 문서 한줄요약(S9, R14).

- 근거는 DART 원문(document.xml)뿐이다. 웹 검색·외부 지식은 쓰지 않는다(프롬프트로 금지).
- 캐시: `summaries`에 접수번호 단위로 저장한다. `ok` 적중이면 LLM을 부르지 않는다.
- 실패 캐시: 원문 조회 실패·원문 없음·LLM 오류는 `failed` + `retry_after = now + SUMMARY_RETRY_DAYS`로
  기록하고, 그 기간에는 다시 호출하지 않는다. 표시는 None(→ `formatting`이 `요약 없음`).
- 근거 부족 응답은 정확히 `요약 없음`으로 정규화해 `ok`로 저장한다(재호출해도 결과가 같다).

`SummaryLlm`·`LlmError`는 이 모듈이 요구하는 포트다. `adapters/llm.py`가 구현한다.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Sequence
from datetime import timedelta
from typing import Protocol

from ipobot.bot.formatting import NO_SUMMARY
from ipobot.clock import Clock
from ipobot.db.repositories import SummaryRepo
from ipobot.domain.models import IpoListing
from ipobot.services.ipo_digest import DartClient, DartUnavailable

log = logging.getLogger(__name__)

SUMMARY_MAX_CHARS = 200
SUMMARY_INPUT_MAX_CHARS = 30_000
_SPACE_RE = re.compile(r"\s+")


class LlmError(Exception):
    """LLM 호출 실패(네트워크·HTTP·응답 구조 오류)."""


class SummaryLlm(Protocol):
    async def summarize(self, corp_name: str, document_text: str) -> str:
        """DART 원문만 근거로 한 한줄요약 원문 응답. 실패는 LlmError."""
        ...


def normalize_summary(raw: str) -> str:
    """LLM 응답 → 한 줄. 비었거나 근거 부족 표시로 시작하면 정확히 `요약 없음`."""
    text = _SPACE_RE.sub(" ", raw).strip().strip("\"'“”‘’").strip()
    if not text or text.startswith(NO_SUMMARY):
        return NO_SUMMARY
    if len(text) > SUMMARY_MAX_CHARS:
        text = text[: SUMMARY_MAX_CHARS - 1].rstrip() + "…"
    return text


class SummaryService:
    def __init__(
        self, llm: SummaryLlm, documents: DartClient, conn: sqlite3.Connection, clock: Clock, retry_days: int
    ) -> None:
        if retry_days < 1:
            raise ValueError("retry_days는 1 이상이어야 합니다")
        self._llm = llm
        self._documents = documents
        self._clock = clock
        self._retry = timedelta(days=retry_days)
        self._repo = SummaryRepo(conn)

    async def summary_for(self, listing: IpoListing) -> str | None:
        """표시할 요약. None이면 `요약 없음`으로 표시한다(실패·재시도 대기)."""
        rcept_no = listing.source_rcept_no
        cached = self._repo.get(rcept_no)
        now = self._clock.now()
        if cached is not None:
            if cached.status == "ok":
                return cached.summary
            assert cached.retry_after is not None
            if now < cached.retry_after:
                return None
        try:
            text = await self._documents.document_text(rcept_no)
            if not text or not text.strip():
                raise LlmError("DART 원문이 없습니다")
            raw = await self._llm.summarize(listing.corp_name, text[:SUMMARY_INPUT_MAX_CHARS])
        except (DartUnavailable, LlmError) as exc:
            log.warning("summary_failed rcept_no=%s corp=%s error=%s", rcept_no, listing.corp_name, exc)
            self._repo.save_failed(rcept_no, listing.corp_code, now + self._retry, now)
            return None
        summary = normalize_summary(raw)
        self._repo.save_ok(rcept_no, listing.corp_code, summary, now)
        return summary

    async def summaries_for(self, listings: Sequence[IpoListing]) -> dict[str, str | None]:
        """접수번호 → 표시 요약. `ipo_digest.digest_items`의 입력이다."""
        return {it.source_rcept_no: await self.summary_for(it) for it in listings}
