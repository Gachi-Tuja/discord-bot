"""예측값 상식 검사(R20 3단계, CRIT-P1-F8). 경고만 하고 확정은 막지 않는다."""

from __future__ import annotations

from fractions import Fraction

IMPLIED_SHARES_RATIO_LIMIT = 10


def implied_shares_warning(pred_price: int, pred_market_cap: int, base_listed_shares: int) -> bool:
    """예측 시총과 '예측 주가 × 기준일 상장주식수'가 10배 이상 차이 나면 True.

    입력이 0 이하이면 비교 근거가 없으므로 경고하지 않는다(금액 자체 검증은 파서가 한다).
    """
    if pred_price <= 0 or pred_market_cap <= 0 or base_listed_shares <= 0:
        return False
    implied = pred_price * base_listed_shares
    ratio = Fraction(max(implied, pred_market_cap), min(implied, pred_market_cap))
    return ratio >= IMPLIED_SHARES_RATIO_LIMIT
