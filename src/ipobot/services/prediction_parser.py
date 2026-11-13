"""예측 자유 텍스트 → 구조화된 4시점 예측(S10, R4·R20 1~2단계, ARCH-08).

흐름: `PredictionLlm.parse_prediction`(tool-use 강제) → pydantic 검증 → 시점 검사(R4) → 금액 검사
(양수, `money.cross_check`). 결과는 `ParsedPrediction` 또는 `ParseRejection`이다. 예외로 거부하지 않는다.

- LLM은 날짜를 만들지 않는다. 기준일·목표일은 S11이 KRX 관측과 `dates.add_months`로 정한다.
- 금액 텍스트가 해석되는데 LLM 정수와 다르면 `AmountMismatch`로 거부(확정 차단)한다.
  해석할 수 없는 텍스트는 경고만 붙이고 통과한다.
- 종목은 사용자가 쓴 그대로(`stock_query`) 넘긴다. 코드/이름 해석(R7)은 S11의 몫이다.
- `LlmError`(호출 실패)는 거부가 아니라 예외로 올린다. 사용자의 입력 문제가 아니기 때문이다.

`PredictionLlm`·`PredictionToolInput`은 이 모듈이 요구하는 포트·도구 스키마다. `adapters/llm.py`가 구현한다.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ipobot.bot.formatting import amount_mismatch_warning, amount_unparseable_warning
from ipobot.domain.models import ParsedPrediction, PredictionTarget
from ipobot.domain.money import cross_check

HORIZONS: tuple[int, ...] = (1, 3, 6, 12)
PREDICTION_TOOL_NAME = "submit_prediction"


class TargetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    horizon_months: int = Field(description="예측 시점(개월). 1, 3, 6, 12 중 하나")
    price_text: str = Field(description="사용자가 쓴 주가 표현 원문 그대로(예: '31,500원', '3만원')")
    price: int = Field(description="price_text를 원 단위 정수로 환산한 값")
    market_cap_text: str = Field(description="사용자가 쓴 시가총액 표현 원문 그대로(예: '3,500억', '1조')")
    market_cap: int = Field(description="market_cap_text를 원 단위 정수로 환산한 값")


class PredictionToolInput(BaseModel):
    """tool `submit_prediction`의 입력 스키마. 날짜 필드는 의도적으로 없다."""

    model_config = ConfigDict(extra="forbid")

    stock: str = Field(min_length=1, description="사용자가 쓴 종목명 또는 종목코드 원문")
    reason: str = Field(min_length=1, description="사용자가 쓴 예측 이유. 요약하지 말고 원문 표현을 살린다")
    targets: list[TargetInput] = Field(description="사용자가 쓴 시점별 예측. 없는 시점을 지어내지 않는다")


class PredictionLlm(Protocol):
    async def parse_prediction(self, text: str) -> Mapping[str, Any] | None:
        """강제 tool 호출의 입력(dict). tool을 호출하지 않았으면 None. 호출 실패는 LlmError."""
        ...


@dataclass(frozen=True)
class ParseRejection:
    message: str


class NoToolCall(ParseRejection):
    pass


class InvalidPayload(ParseRejection):
    pass


class MissingHorizon(ParseRejection):
    pass


class DuplicateHorizon(ParseRejection):
    pass


class InvalidAmount(ParseRejection):
    pass


class AmountMismatch(ParseRejection):
    pass


NO_TOOL_CALL_TEXT = "예측 내용을 해석하지 못했습니다. 종목, 1·3·6·12개월 주가·시총, 이유를 적어 주세요"
INVALID_PAYLOAD_TEXT = "예측 내용을 해석하지 못했습니다. 다시 작성해 주세요"


def _months(horizons: list[int]) -> str:
    return ", ".join(f"{h}개월" for h in sorted(horizons))


def validate_prediction(payload: Mapping[str, Any] | None) -> ParsedPrediction | ParseRejection:
    """tool 입력 → 검증 결과. 순수 함수."""
    if payload is None:
        return NoToolCall(NO_TOOL_CALL_TEXT)
    try:
        data = PredictionToolInput.model_validate(payload)
    except ValidationError:
        return InvalidPayload(INVALID_PAYLOAD_TEXT)
    stock, reason = data.stock.strip(), data.reason.strip()
    if not stock or not reason:
        return InvalidPayload(INVALID_PAYLOAD_TEXT)

    horizons = [t.horizon_months for t in data.targets]
    unknown = sorted({h for h in horizons if h not in HORIZONS})
    if unknown:
        return InvalidPayload(f"지원하지 않는 시점: {_months(unknown)} (1·3·6·12개월만 가능)")
    duplicated = sorted(h for h, n in Counter(horizons).items() if n > 1)
    if duplicated:
        return DuplicateHorizon(f"중복된 시점: {_months(duplicated)}")
    missing = [h for h in HORIZONS if h not in horizons]
    if missing:
        return MissingHorizon(f"누락된 시점: {_months(missing)}")

    targets = sorted(data.targets, key=lambda t: t.horizon_months)
    bad = [
        f"{t.horizon_months}개월 {label}"
        for t in targets
        for label, value in (("주가", t.price), ("시총", t.market_cap))
        if value <= 0
    ]
    if bad:
        return InvalidAmount(f"금액은 0보다 커야 합니다: {', '.join(bad)}")

    mismatches: list[str] = []
    warnings: list[str] = []
    for t in targets:
        for text, value in ((t.price_text, t.price), (t.market_cap_text, t.market_cap)):
            check = cross_check(text, value)
            if check.status == "mismatch":
                mismatches.append(amount_mismatch_warning(text, value))
            elif check.status == "unparseable":
                warnings.append(amount_unparseable_warning(text))
    if mismatches:
        return AmountMismatch("\n".join(mismatches))

    return ParsedPrediction(
        stock_query=stock,
        reason=reason,
        targets=tuple(
            PredictionTarget(
                horizon_months=t.horizon_months,
                pred_price=t.price,
                pred_market_cap=t.market_cap,
                price_text=t.price_text.strip(),
                market_cap_text=t.market_cap_text.strip(),
            )
            for t in targets
        ),
        warnings=tuple(warnings),
    )


class PredictionParser:
    def __init__(self, llm: PredictionLlm) -> None:
        self._llm = llm

    async def parse(self, text: str) -> ParsedPrediction | ParseRejection:
        if not text.strip():
            return NoToolCall(NO_TOOL_CALL_TEXT)
        return validate_prediction(await self._llm.parse_prediction(text))
