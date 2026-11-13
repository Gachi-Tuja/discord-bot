"""Anthropic 어댑터. `services.summaries.SummaryLlm` 포트를 구현한다.

- `AsyncAnthropic`만 쓴다. `client`를 넘기면(테스트 fake 포함) 그 객체의 `messages.create`를 호출한다.
- SDK 오류(`anthropic.APIError`: 연결·타임아웃·HTTP 상태)와 응답 구조 오류는 모두 `LlmError`다.
- 요약 프롬프트는 DART 원문 밖의 지식·추측을 금지하고, 근거가 부족하면 정확히 `요약 없음`을 요구한다.
"""

from __future__ import annotations

from typing import Any

import anthropic

from ipobot.bot.formatting import NO_SUMMARY
from ipobot.services.summaries import LlmError

SUMMARY_MAX_TOKENS = 300
SUMMARY_SYSTEM_PROMPT = (
    "당신은 한국 공모주 증권신고서를 요약하는 도우미다. 사용자가 준 DART 공시 원문만 근거로 삼는다. "
    "원문에 없는 사실, 외부 지식, 추측, 투자 권유를 쓰지 않는다. "
    "회사가 무슨 사업을 하는지 한국어 한 문장(80자 이내)으로만 답한다. 머리말·따옴표·목록을 붙이지 않는다. "
    f"원문만으로 사업 내용을 알 수 없으면 다른 말 없이 정확히 `{NO_SUMMARY}`라고만 답한다."
)


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
