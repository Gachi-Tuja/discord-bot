"""S5: 오차율(Fraction)과 그룹 순위."""

from __future__ import annotations

from fractions import Fraction

import pytest

from ipobot.domain.scoring import GroupEntry, InvalidActual, error_ratio, fraction_parts, rank_group


def test_error_ratio_basic():
    assert error_ratio(11_000, 10_000) == Fraction(1, 10)
    assert error_ratio(9_000, 10_000) == Fraction(1, 10)
    assert error_ratio(10_000, 10_000) == 0


@pytest.mark.parametrize("actual", [0, -1])
def test_error_ratio_invalid_actual(actual):
    with pytest.raises(InvalidActual):
        error_ratio(1, actual)


def test_rank_general():
    entries = [GroupEntry(1, 12_000, 100), GroupEntry(2, 10_500, 100), GroupEntry(3, 7_000, 100)]
    out = rank_group(entries, 10_000, 100)
    assert [(s.prediction_id, s.rank) for s in out] == [(2, 1), (1, 2), (3, 3)]
    assert all(not s.is_solo for s in out)
    assert out[0].price_err == Fraction(1, 20)


def test_exact_tie_shares_rank():
    entries = [GroupEntry(5, 11_000, 1), GroupEntry(3, 9_000, 2), GroupEntry(9, 10_300, 3)]
    out = rank_group(entries, 10_000, 10)
    assert [(s.prediction_id, s.rank) for s in out] == [(9, 1), (3, 2), (5, 2)]
    tie = rank_group([GroupEntry(1, 11_000, 1), GroupEntry(2, 9_000, 1), GroupEntry(3, 13_000, 1)], 10_000, 1)
    assert [(s.prediction_id, s.rank) for s in tie] == [(1, 1), (2, 1), (3, 3)]


def test_near_tie_float_would_misjudge():
    # 실제 0.3: |0.1+0.2 − 0.3| 같은 부동소수 오차 대신 정확한 유리수로 비교한다.
    actual = 3
    a = GroupEntry(1, 4, 1)  # 1/3
    b = GroupEntry(2, 2, 1)  # 1/3 → 정확 동점
    out = rank_group([a, b], actual, 1)
    assert [s.rank for s in out] == [1, 1]
    # 근소차: 부동소수점으로는 같아 보이는 두 오차
    actual = 10**17
    c = GroupEntry(3, 10**17 + 1, 1)
    d = GroupEntry(4, 10**17 + 2, 1)
    assert float(error_ratio(c.pred_price, actual)) + 1 == float(error_ratio(d.pred_price, actual)) + 1
    out = rank_group([d, c], actual, 1)
    assert [(s.prediction_id, s.rank) for s in out] == [(3, 1), (4, 2)]


def test_solo():
    (only,) = rank_group([GroupEntry(7, 1, 1)], 10, 10)
    assert only.rank == 1 and only.is_solo is True


def test_fraction_parts_den_positive():
    out = rank_group([GroupEntry(1, 5, 0), GroupEntry(2, 10, 20)], 10, 20)
    for s in out:
        for num, den in (s.price_err_parts, s.mcap_err_parts):
            assert den > 0 and num >= 0
            assert Fraction(num, den) in (s.price_err, s.mcap_err)
    assert fraction_parts(Fraction(0)) == (0, 1)
    assert fraction_parts(Fraction(2, 4)) == (1, 2)


def test_rank_group_rejects_empty_duplicate_and_invalid():
    with pytest.raises(ValueError):
        rank_group([], 1, 1)
    with pytest.raises(ValueError):
        rank_group([GroupEntry(1, 1, 1), GroupEntry(1, 2, 2)], 1, 1)
    with pytest.raises(InvalidActual):
        rank_group([GroupEntry(1, 1, 1)], 0, 1)
