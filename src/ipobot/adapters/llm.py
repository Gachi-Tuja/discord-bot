"""Anthropic 어댑터. `services.summaries.SummaryLlm`·`services.prediction_parser.PredictionLlm` 포트를 구현한다.

- `AsyncAnthropic`만 쓴다. `client`를 넘기면(테스트 fake 포함) 그 객체의 `messages.create`를 호출한다.
- SDK 오류(`anthropic.APIError`: 연결·타임아웃·HTTP 상태)와 응답 구조 오류는 모두 `LlmError`다.
- 예측 파싱은 `tool_choice`로 `submit_prediction` 호출을 강제한다. 스키마는 pydantic 모델에서 만든다.
  응답에 그 tool_use 블록이 없으면 None(거부는 서비스가 정한다).
- 요약 프롬프트는 DART 원문 밖의 지식·추측을 금지하고, 근거가 부족하면 정확히 `요약 없음`을 요구한다.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import anthropic

from ipobot.bot.formatting import NO_SUMMARY
from ipobot.services.prediction_parser import PREDICTION_TOOL_NAME, PredictionToolInput
from ipobot.services.summaries import LlmError

SUMMARY_MAX_TOKENS = 300
SUMMARY_SYSTEM_PROMPT = (
    "당신은 한국 공모주 증권신고서를 요약하는 도우미다. 사용자가 준 DART 공시 원문만 근거로 삼는다. "
    "원문에 없는 사실, 외부 지식, 추측, 투자 권유를 쓰지 않는다. "
    "회사가 무슨 사업을 하는지 한국어 한 문장(80자 이내)으로만 답한다. 머리말·따옴표·목록을 붙이지 않는다. "
    f"원문만으로 사업 내용을 알 수 없으면 다른 말 없이 정확히 `{NO_SUMMARY}`라고만 답한다."
)

PARSE_MAX_TOKENS = 2000
PARSE_SYSTEM_PROMPT = (
    "당신은 한국 주식 예측 대회의 입력을 구조화하는 도우미다. 사용자의 자유 텍스트에서 종목, "
    "1·3·6·12개월 시점별 예측 주가·시가총액, 예측 이유를 뽑아 submit_prediction 도구로 제출한다. "
    "규칙: 사용자가 쓰지 않은 시점이나 금액을 지어내지 않는다(없는 시점은 targets에서 뺀다). "
    "price_text·market_cap_text에는 사용자가 쓴 금액 표현을 원문 그대로 옮기고, price·market_cap에는 그 표현을 "
    "원 단위 정수로 정확히 환산한다(예: '3,500억' → 350000000000, '3만원' → 30000, '1조' → 1000000000000). "
    "범위로 쓴 금액(예: '5~6만원')은 텍스트를 그대로 두고 정수는 범위 안의 대표값으로 한다. "
    "날짜는 만들지 않는다. 이유는 요약하지 말고 사용자의 표현을 살린다."
)
PREDICTION_TOOL: dict[str, Any] = {
    "name": PREDICTION_TOOL_NAME,
    "description": "사용자의 주가·시가총액 예측을 구조화해 제출한다.",
    "input_schema": PredictionToolInput.model_json_schema(),
}


def _text_of(message: Any) -> str:
    try:
        blocks = message.content
    except AttributeError as exc:
        raise LlmError("응답에 content가 없습니다") from exc
    return "".join(getattr(b, "text", "") for b in blocks if getattr(b, "type", None) == "text")


class AnthropicLlmClient:
    def __init__(
        self,
        api_key: str,
        *,
        summary_model: str,
        parse_model: str,
        client: Any | None = None,
    ) -> None:
        self._client = client if client is not None else anthropic.AsyncAnthropic(api_key=api_key)
        self._summary_model = summary_model
        self._parse_model = parse_model

    async def _create(self, **kwargs: Any) -> Any:
        try:
            return await self._client.messages.create(**kwargs)
        except anthropic.APIError as exc:
            raise LlmError(f"Anthropic 호출 실패: {type(exc).__name__}") from exc

    async def summarize(self, corp_name: str, document_text: str) -> str:
        message = await self._create(
            model=self._summary_model,
            max_tokens=SUMMARY_MAX_TOKENS,
            system=SUMMARY_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"회사명: {corp_name}\n\n<dart_document>\n{document_text}\n</dart_document>",
                }
            ],
        )
        text = _text_of(message)
        if not text.strip():
            raise LlmError("요약 응답에 텍스트가 없습니다")
        return text

    async def parse_prediction(self, text: str) -> Mapping[str, Any] | None:
        message = await self._create(
            model=self._parse_model,
            max_tokens=PARSE_MAX_TOKENS,
            system=PARSE_SYSTEM_PROMPT,
            tools=[PREDICTION_TOOL],
            tool_choice={"type": "tool", "name": PREDICTION_TOOL_NAME},
            messages=[{"role": "user", "content": f"<prediction>\n{text}\n</prediction>"}],
        )
        try:
            blocks = message.content
        except AttributeError as exc:
            raise LlmError("응답에 content가 없습니다") from exc
        for block in blocks:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == PREDICTION_TOOL_NAME:
                payload = getattr(block, "input", None)
                return payload if isinstance(payload, Mapping) else None
        return None
