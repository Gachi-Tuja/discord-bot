"""S10: 예측 파서(R4·R20·ARCH-08)와 Anthropic tool-use 강제(fake 클라이언트). 실제 외부 호출 없음."""

from __future__ import annotations

import copy

import httpx
import pytest
import anthropic
from anthropic.types import Message, TextBlock, ToolUseBlock, Usage

from ipobot.adapters.llm import PARSE_SYSTEM_PROMPT, PREDICTION_TOOL, AnthropicLlmClient
from ipobot.domain.models import ParsedPrediction, PredictionTarget
from ipobot.services.prediction_parser import (
    PREDICTION_TOOL_NAME,
    AmountMismatch,
    DuplicateHorizon,
    InvalidAmount,
    InvalidPayload,
    MissingHorizon,
    NoToolCall,
    PredictionParser,
    validate_prediction,
)
from ipobot.services.summaries import LlmError

# 스펙 원본 예시(Chungshik/앨리스)의 사용자 입력
ORIGINAL_TEXT = """Chungshik님 예측 — 앨리스
1개월: 주가 31,500원 / 시총 3,500억
3개월: 주가 90,000원 / 시총 1조
6개월: 주가 54,000원 / 시총 6,000억
12개월: 주가 45,000원 / 시총 5,000억
이유: 상장 초기 유통물량이 적어 3개월까지 급등하고, 6개월 차에는 보호예수 해제로 5만~6만원 범위에서 등락할 것.
12개월 시점은 매출 성장률 30%와 동종업계 PER 25배를 근거로 시총 5,000억 수준에 수렴한다고 봄."""

REASON = ORIGINAL_TEXT.split("이유: ", 1)[1]


def original_payload() -> dict:
    return {
        "stock": "앨리스",
        "reason": REASON,
        "targets": [
            # LLM이 순서를 섞어도 결과는 시점 순이다
            {"horizon_months": 12, "price_text": "45,000원", "price": 45_000, "market_cap_text": "5,000억", "market_cap": 500_000_000_000},
            {"horizon_months": 1, "price_text": "31,500원", "price": 31_500, "market_cap_text": "3,500억", "market_cap": 350_000_000_000},
            {"horizon_months": 3, "price_text": "90,000원", "price": 90_000, "market_cap_text": "1조", "market_cap": 1_000_000_000_000},
            {"horizon_months": 6, "price_text": "54,000원", "price": 54_000, "market_cap_text": "6,000억", "market_cap": 600_000_000_000},
        ],
    }


def with_target(payload: dict, horizon: int, **changes) -> dict:
    p = copy.deepcopy(payload)
    for t in p["targets"]:
        if t["horizon_months"] == horizon:
            t.update(changes)
    return p


def test_original_example_four_horizons_and_reason():
    result = validate_prediction(original_payload())
    assert isinstance(result, ParsedPrediction)
    assert result.stock_query == "앨리스"
    assert result.warnings == ()
    assert result.targets == (
        PredictionTarget(1, 31_500, 350_000_000_000, "31,500원", "3,500억"),
        PredictionTarget(3, 90_000, 1_000_000_000_000, "90,000원", "1조"),
        PredictionTarget(6, 54_000, 600_000_000_000, "54,000원", "6,000억"),
        PredictionTarget(12, 45_000, 500_000_000_000, "45,000원", "5,000억"),
    )
    assert "5만~6만원 범위" in result.reason  # 6개월 이유의 범위
    assert "PER 25배를 근거로" in result.reason  # 12개월 이유의 근거


def test_missing_horizon_rejected():
    p = original_payload()
    p["targets"] = [t for t in p["targets"] if t["horizon_months"] != 6]
    result = validate_prediction(p)
    assert result == MissingHorizon("누락된 시점: 6개월")


def test_multiple_missing_horizons_listed_in_order():
    p = original_payload()
    p["targets"] = [t for t in p["targets"] if t["horizon_months"] == 3]
    assert validate_prediction(p) == MissingHorizon("누락된 시점: 1개월, 6개월, 12개월")


def test_duplicate_horizon_rejected():
    p = original_payload()
    p["targets"].append(dict(p["targets"][1]))
    assert validate_prediction(p) == DuplicateHorizon("중복된 시점: 1개월")


def test_unknown_horizon_rejected():
    p = with_target(original_payload(), 3, horizon_months=2)
    result = validate_prediction(p)
    assert isinstance(result, InvalidPayload) and "2개월" in result.message


def test_no_tool_call_rejected():
    assert isinstance(validate_prediction(None), NoToolCall)


@pytest.mark.parametrize(
    ("field", "value", "label"),
    [("price", 0, "1개월 주가"), ("price", -31_500, "1개월 주가"), ("market_cap", 0, "1개월 시총"), ("market_cap", -1, "1개월 시총")],
)
def test_non_positive_amount_rejected(field, value, label):
    result = validate_prediction(with_target(original_payload(), 1, **{field: value}))
    assert isinstance(result, InvalidAmount) and label in result.message


def test_amount_text_integer_mismatch_rejected():
    # ARCH-08 명시 사례: '3,500억'인데 LLM이 350억으로 환산
    result = validate_prediction(with_target(original_payload(), 1, market_cap=35_000_000_000))
    assert isinstance(result, AmountMismatch)
    assert result.message.startswith("금액 해석 불일치: 텍스트 3,500억 ↔ 해석 ")


def test_range_text_out_of_range_rejected_and_in_range_ok():
    ok = validate_prediction(with_target(original_payload(), 6, price_text="5~6만원", price=55_000))
    assert isinstance(ok, ParsedPrediction) and ok.targets[2].price_text == "5~6만원"
    bad = validate_prediction(with_target(original_payload(), 6, price_text="5~6만원", price=65_000))
    assert isinstance(bad, AmountMismatch)


def test_unparseable_text_succeeds_with_warning():
    result = validate_prediction(with_target(original_payload(), 12, market_cap_text="오천억 정도"))
    assert isinstance(result, ParsedPrediction)
    assert result.warnings == ("⚠️ 금액 텍스트를 해석하지 못했습니다: 오천억 정도 (확정은 가능)",)
    assert result.targets[3].pred_market_cap == 500_000_000_000


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("stock"),
        lambda p: p.update(stock="   "),
        lambda p: p.update(reason=""),
        lambda p: p.update(targets="1개월 31500"),
        lambda p: p["targets"][0].update(price="31,500"),
        lambda p: p["targets"][0].pop("price_text"),
        lambda p: p.update(target_date="2026-12-10"),  # 날짜 필드는 받지 않는다
    ],
)
def test_malformed_payload_rejected(mutate):
    p = original_payload()
    mutate(p)
    assert isinstance(validate_prediction(p), InvalidPayload)


# ---------------------------------------------------------------- PredictionParser + Anthropic 어댑터


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


def message(*blocks, stop_reason="tool_use") -> Message:
    return Message(
        id="msg_1",
        type="message",
        role="assistant",
        model="claude-test",
        content=list(blocks),
        stop_reason=stop_reason,
        stop_sequence=None,
        usage=Usage(input_tokens=10, output_tokens=5),
    )


def tool_use(payload, name=PREDICTION_TOOL_NAME) -> ToolUseBlock:
    return ToolUseBlock(type="tool_use", id="toolu_1", name=name, input=payload)


def parser_with(result) -> tuple[PredictionParser, FakeAnthropic]:
    fake = FakeAnthropic(result)
    llm = AnthropicLlmClient("key", summary_model="sum-model", parse_model="parse-model", client=fake)
    return PredictionParser(llm), fake


async def test_adapter_forces_tool_use_and_parses_original_example():
    parser, fake = parser_with(message(tool_use(original_payload())))
    result = await parser.parse(ORIGINAL_TEXT)
    assert isinstance(result, ParsedPrediction) and [t.horizon_months for t in result.targets] == [1, 3, 6, 12]
    [kwargs] = fake.messages.kwargs
    assert kwargs["model"] == "parse-model"
    assert kwargs["system"] == PARSE_SYSTEM_PROMPT
    assert kwargs["tools"] == [PREDICTION_TOOL]
    assert kwargs["tool_choice"] == {"type": "tool", "name": PREDICTION_TOOL_NAME}
    assert ORIGINAL_TEXT in kwargs["messages"][0]["content"]
    schema = PREDICTION_TOOL["input_schema"]
    assert set(schema["required"]) == {"stock", "reason", "targets"}
    assert "date" not in str(schema).lower()


@pytest.mark.parametrize(
    "response",
    [
        message(TextBlock(type="text", text="예측을 이해하지 못했습니다"), stop_reason="end_turn"),
        message(tool_use(original_payload(), name="other_tool")),
        message(),
    ],
)
async def test_adapter_without_matching_tool_call_is_rejected(response):
    parser, _ = parser_with(response)
    assert isinstance(await parser.parse(ORIGINAL_TEXT), NoToolCall)


async def test_adapter_mismatch_flows_to_amount_mismatch():
    parser, _ = parser_with(message(tool_use(with_target(original_payload(), 1, market_cap=35_000_000_000))))
    assert isinstance(await parser.parse(ORIGINAL_TEXT), AmountMismatch)


async def test_blank_text_rejected_without_llm_call():
    parser, fake = parser_with(message(tool_use(original_payload())))
    assert isinstance(await parser.parse("  \n "), NoToolCall)
    assert fake.messages.kwargs == []


async def test_adapter_api_error_raises_llm_error():
    request = httpx.Request("POST", "https://api.anthropic.test/v1/messages")
    parser, _ = parser_with(anthropic.APITimeoutError(request=request))
    with pytest.raises(LlmError):
        await parser.parse(ORIGINAL_TEXT)


def test_huge_amount_texts_keep_messages_within_discord_limit():
    payload = original_payload()
    for t in payload["targets"]:
        t["price_text"] = "1" * 5000 + "원"
        t["market_cap_text"] = "30,000원" + " " * 5000 + "x" * 5000
    result = validate_prediction(payload)
    text = result.message if hasattr(result, "message") else "\n".join(result.warnings)
    assert len(text) <= 2000, len(text)
    assert "1" * 41 not in text


def test_huge_unparseable_texts_keep_warnings_short():
    long = "대략 이 정도" + "~" * 5000
    result = validate_prediction(with_target(original_payload(), 12, market_cap_text=long))
    assert isinstance(result, ParsedPrediction)
    (warning,) = result.warnings
    assert len(warning) < 100 and warning.count("~") < 40
