"""S9: DART 요약 캐시·실패 캐시(R14)와 Anthropic 어댑터(fake 클라이언트). 실제 외부 호출 없음."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import anthropic
import httpx
import pytest
from anthropic.types import Message, TextBlock, Usage

from ipobot.adapters.llm import SUMMARY_SYSTEM_PROMPT, AnthropicLlmClient
from ipobot.bot.formatting import NO_SUMMARY, digest_parts
from ipobot.clock import KST
from ipobot.db.connection import open_database
from ipobot.db.repositories import SummaryRepo
from ipobot.domain.models import IpoListing
from ipobot.services.ipo_digest import DartUnavailable, digest_items
from ipobot.services.summaries import (
    SUMMARY_INPUT_MAX_CHARS,
    SUMMARY_MAX_CHARS,
    LlmError,
    SummaryService,
    normalize_summary,
)

RCEPT = "20261020000123"
LISTING = IpoListing(
    corp_code="01987654",
    corp_name="가치토자바이오",
    listing_date=date(2026, 11, 25),
    source_rcept_no=RCEPT,
    market="KOSDAQ",
)


class FakeLlm:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    async def summarize(self, corp_name, document_text):
        self.calls.append((corp_name, document_text))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakeDocs:
    def __init__(self, text="원문: 항체 신약을 개발하는 바이오 기업이다."):
        self.text = text
        self.calls: list[str] = []

    async def document_text(self, rcept_no):
        self.calls.append(rcept_no)
        if isinstance(self.text, Exception):
            raise self.text
        return self.text


@pytest.fixture
def conn():
    c = open_database(":memory:")
    yield c
    c.close()


def service(llm, docs, conn, clock, retry_days=3):
    return SummaryService(llm, docs, conn, clock, retry_days)


async def test_cache_hit_makes_zero_llm_calls(conn, fake_clock):
    llm, docs = FakeLlm("항체 신약 개발 바이오 기업"), FakeDocs()
    svc = service(llm, docs, conn, fake_clock)
    assert await svc.summary_for(LISTING) == "항체 신약 개발 바이오 기업"
    assert await svc.summary_for(LISTING) == "항체 신약 개발 바이오 기업"
    assert len(llm.calls) == 1 and len(docs.calls) == 1
    rec = SummaryRepo(conn).get(RCEPT)
    assert (rec.status, rec.summary, rec.retry_after) == ("ok", "항체 신약 개발 바이오 기업", None)
    # 새 서비스 인스턴스(재시작)에서도 DB 캐시가 적중한다
    llm2 = FakeLlm()
    assert await service(llm2, FakeDocs(), conn, fake_clock).summary_for(LISTING) == "항체 신약 개발 바이오 기업"
    assert llm2.calls == []


@pytest.mark.parametrize(
    ("llm_responses", "docs_text"),
    [
        ((LlmError("Anthropic 호출 실패: APITimeoutError"),), "원문"),
        ((), DartUnavailable("document.xml HTTP 503")),
        ((), None),
        ((), "   "),
    ],
)
async def test_failure_is_cached_until_retry_after(conn, fake_clock, llm_responses, docs_text):
    llm, docs = FakeLlm(*llm_responses, "재시도 성공 요약"), FakeDocs(docs_text)
    svc = service(llm, docs, conn, fake_clock, retry_days=3)
    assert await svc.summary_for(LISTING) is None
    rec = SummaryRepo(conn).get(RCEPT)
    assert rec.status == "failed" and rec.summary is None
    assert rec.retry_after == fake_clock.now() + timedelta(days=3)
    calls_after_failure = (len(llm.calls), len(docs.calls))

    # 기간 안: 재호출 0회, 표시는 요약 없음
    fake_clock.set(fake_clock.now() + timedelta(days=3) - timedelta(seconds=1))
    assert await svc.summary_for(LISTING) is None
    assert (len(llm.calls), len(docs.calls)) == calls_after_failure
    [part] = digest_parts(digest_items([LISTING], {RCEPT: None}), date(2026, 11, 12))
    assert part.embeds[0]["fields"][0]["value"] == NO_SUMMARY

    # 기간 경과 후 재시도하고 성공하면 ok로 덮어쓴다
    docs.text = "원문: 로봇 제조 기업"
    fake_clock.set(fake_clock.now() + timedelta(seconds=1))
    assert await svc.summary_for(LISTING) == "재시도 성공 요약"
    rec = SummaryRepo(conn).get(RCEPT)
    assert (rec.status, rec.summary, rec.retry_after) == ("ok", "재시도 성공 요약", None)


@pytest.mark.parametrize("raw", ["요약 없음", "  요약 없음.  ", "\"요약 없음\"", "요약 없음 (원문에 사업 내용이 없습니다)", "", "\n"])
async def test_insufficient_basis_is_exactly_no_summary(conn, fake_clock, raw):
    llm = FakeLlm(raw)
    svc = service(llm, FakeDocs(), conn, fake_clock)
    assert await svc.summary_for(LISTING) == "요약 없음"
    rec = SummaryRepo(conn).get(RCEPT)
    assert (rec.status, rec.summary) == ("ok", "요약 없음")
    assert await svc.summary_for(LISTING) == "요약 없음"
    assert len(llm.calls) == 1


def test_normalize_summary_one_line_and_capped():
    assert normalize_summary("  항체 신약\n개발 기업  ") == "항체 신약 개발 기업"
    long = normalize_summary("가" * 500)
    assert len(long) == SUMMARY_MAX_CHARS and long.endswith("…")


async def test_document_text_is_truncated_for_llm(conn, fake_clock):
    llm = FakeLlm("요약")
    await service(llm, FakeDocs("나" * (SUMMARY_INPUT_MAX_CHARS + 50)), conn, fake_clock).summary_for(LISTING)
    assert llm.calls == [("가치토자바이오", "나" * SUMMARY_INPUT_MAX_CHARS)]


@pytest.mark.parametrize("heading", ["II. 사업의 내용", "Ⅱ. 사업의 내용", "II.사업의  내용"])
async def test_llm_input_starts_at_last_business_section(conn, fake_clock, heading):
    # 투자설명서 앞부분은 정정표·공모조건이고, 앞쪽의 같은 제목은 목차·참조다. 본문 섹션(마지막)부터 넣는다.
    body = f"{heading} 당사는 로봇용 힘센서를 만듭니다." + "다" * SUMMARY_INPUT_MAX_CHARS
    text = "정정표" * 20_000 + f"목차 {heading} 3쪽 " + "공모" * 5_000 + body
    llm = FakeLlm("로봇용 힘센서 제조.")
    await service(llm, FakeDocs(text), conn, fake_clock).summary_for(LISTING)
    assert llm.calls == [("가치토자바이오", body[:SUMMARY_INPUT_MAX_CHARS])]


async def test_summaries_for_maps_rcept_numbers(conn, fake_clock):
    other = IpoListing("C2", "샘플로보틱스", date(2026, 11, 20), "20261028000456")
    llm = FakeLlm("바이오 요약", LlmError("down"))
    result = await service(llm, FakeDocs(), conn, fake_clock).summaries_for([LISTING, other])
    assert result == {RCEPT: "바이오 요약", "20261028000456": None}


def test_service_rejects_bad_retry_days(conn, fake_clock):
    with pytest.raises(ValueError):
        service(FakeLlm(), FakeDocs(), conn, fake_clock, retry_days=0)


def test_repo_failed_requires_aware_retry_after(conn):
    with pytest.raises(ValueError):
        SummaryRepo(conn).save_failed(RCEPT, "C", datetime(2026, 1, 1), datetime(2026, 1, 1, tzinfo=KST))


# ---------------------------------------------------------------- Anthropic 어댑터 (fake AsyncAnthropic)


def message(*blocks) -> Message:
    return Message(
        id="msg_1",
        type="message",
        role="assistant",
        model="claude-test",
        content=list(blocks),
        stop_reason="end_turn",
        stop_sequence=None,
        usage=Usage(input_tokens=10, output_tokens=5),
    )


class FakeMessages:
    def __init__(self, result):
        self.result = result
        self.kwargs: list[dict] = []

    async def create(self, **kwargs):
        self.kwargs.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeAnthropic:
    def __init__(self, result):
        self.messages = FakeMessages(result)
        self.closed = 0

    async def close(self):
        self.closed += 1


def llm_client(result) -> tuple[AnthropicLlmClient, FakeAnthropic]:
    fake = FakeAnthropic(result)
    return AnthropicLlmClient("key", summary_model="sum-model", parse_model="parse-model", client=fake), fake


async def test_adapter_aclose_closes_underlying_client():
    client, fake = llm_client(None)
    await client.aclose()
    assert fake.closed == 1


async def test_adapter_summarize_request_and_text():
    client, fake = llm_client(message(TextBlock(type="text", text="항체 신약 "), TextBlock(type="text", text="개발 기업")))
    assert await client.summarize("가치토자바이오", "원문 본문") == "항체 신약 개발 기업"
    [kwargs] = fake.messages.kwargs
    assert kwargs["model"] == "sum-model"
    assert kwargs["system"] == SUMMARY_SYSTEM_PROMPT and "요약 없음" in SUMMARY_SYSTEM_PROMPT
    assert "tools" not in kwargs
    content = kwargs["messages"][0]["content"]
    assert "가치토자바이오" in content and "<dart_document>\n원문 본문\n</dart_document>" in content


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://api.anthropic.test/v1/messages")


@pytest.mark.parametrize(
    "error",
    [
        anthropic.APITimeoutError(request=_request()),
        anthropic.APIConnectionError(request=_request()),
        anthropic.InternalServerError("boom", response=httpx.Response(500, request=_request()), body=None),
        anthropic.RateLimitError("slow", response=httpx.Response(429, request=_request()), body=None),
    ],
)
async def test_adapter_api_errors_become_llm_error(error):
    client, _ = llm_client(error)
    with pytest.raises(LlmError):
        await client.summarize("회사", "원문")


async def test_adapter_empty_text_is_llm_error():
    client, _ = llm_client(message())
    with pytest.raises(LlmError):
        await client.summarize("회사", "원문")


async def test_adapter_failure_flows_into_failed_cache(conn, fake_clock):
    client, _ = llm_client(anthropic.APITimeoutError(request=_request()))
    svc = SummaryService(client, FakeDocs(), conn, fake_clock, 3)
    assert await svc.summary_for(LISTING) is None
    assert SummaryRepo(conn).get(RCEPT).status == "failed"
