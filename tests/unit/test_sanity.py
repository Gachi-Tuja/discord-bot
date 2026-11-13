"""S4: LIST_SHRS 10배 편차 경고."""

from __future__ import annotations

from ipobot.domain.sanity import implied_shares_warning

EOK = 10**8


def test_consistent_values_no_warning():
    assert implied_shares_warning(69_800, 7_768 * EOK, 11_128_939) is False


def test_tenfold_typo_warns():
    assert implied_shares_warning(69_800, 77_680 * EOK, 11_128_939) is True


def test_tenfold_too_small_warns():
    assert implied_shares_warning(69_800, 776 * EOK, 11_128_939) is True


def test_just_under_ratio_no_warning():
    # implied = 1,000 * 1,000 = 1,000,000; 9,999,999 / 1,000,000 < 10
    assert implied_shares_warning(1_000, 9_999_999, 1_000) is False
    assert implied_shares_warning(1_000, 10_000_000, 1_000) is True


def test_non_positive_inputs_no_warning():
    assert implied_shares_warning(0, 1, 1) is False
    assert implied_shares_warning(1, 1, 0) is False
