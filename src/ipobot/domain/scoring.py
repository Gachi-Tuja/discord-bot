"""오차율과 그룹 순위(R10, R11). 부동소수점 없이 Fraction으로 계산한다."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction


class InvalidActual(ValueError):
    """실제값이 0 이하라 오차율을 정의할 수 없다(방어 코드, R10 `invalid_actual`)."""


def error_ratio(predicted: int, actual: int) -> Fraction:
    """|predicted − actual| / actual."""
    if actual <= 0:
        raise InvalidActual(f"실제값은 양수여야 합니다: {actual}")
    return Fraction(abs(predicted - actual), actual)


def fraction_parts(value: Fraction) -> tuple[int, int]:
    """DB 저장용 (num, den). Fraction은 항상 기약분수·den > 0이다."""
    return value.numerator, value.denominator


@dataclass(frozen=True)
class GroupEntry:
    prediction_id: int
    pred_price: int
    pred_market_cap: int


@dataclass(frozen=True)
class ScoredEntry:
    prediction_id: int
    price_err: Fraction
    mcap_err: Fraction
    rank: int
    is_solo: bool

    @property
    def price_err_parts(self) -> tuple[int, int]:
        return fraction_parts(self.price_err)

    @property
    def mcap_err_parts(self) -> tuple[int, int]:
        return fraction_parts(self.mcap_err)


def rank_group(entries: Sequence[GroupEntry], actual_price: int, actual_market_cap: int) -> list[ScoredEntry]:
    """주가 오차 오름차순 경쟁 순위(1,1,3). 동점은 공동 순위, 1명이면 is_solo.

    반환 순서는 (rank, prediction_id)다. 실제값이 0 이하면 InvalidActual.
    """
    if not entries:
        raise ValueError("빈 그룹은 채점할 수 없습니다")
    ids = [e.prediction_id for e in entries]
    if len(set(ids)) != len(ids):
        raise ValueError("그룹에 같은 예측이 중복되어 있습니다")
    scored = [
        (e, error_ratio(e.pred_price, actual_price), error_ratio(e.pred_market_cap, actual_market_cap))
        for e in entries
    ]
    scored.sort(key=lambda t: (t[1], t[0].prediction_id))
    solo = len(scored) == 1
    out: list[ScoredEntry] = []
    prev_err: Fraction | None = None
    rank = 0
    for position, (entry, price_err, mcap_err) in enumerate(scored, start=1):
        if price_err != prev_err:
            rank = position
            prev_err = price_err
        out.append(ScoredEntry(entry.prediction_id, price_err, mcap_err, rank, solo))
    return out
