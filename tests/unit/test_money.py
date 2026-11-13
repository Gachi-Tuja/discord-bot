"""S4: 금액 텍스트 해석과 교차검증(R20, ARCH-08)."""

from __future__ import annotations

import pytest

from ipobot.domain.money import Exact, Range, Unparseable, cross_check, parse_krw_text

EOK = 10**8
JO = 10**12


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("69,800원", 69_800),
        ("69800", 69_800),
        ("5만원", 50_000),
        ("3,500억", 3_500 * EOK),
        ("3,500억원", 3_500 * EOK),
        ("1조 2,000억", 1_200_000_000_000),
        ("1조2000억원", 1_200_000_000_000),
        ("1.5조", 1_500_000_000_000),
        ("5천억", 500_000_000_000),
        ("1조", JO),
        ("약 31,500원", 31_500),
        ("2.5만원", 25_000),
        ("5천원", 5_000),
        ("1억 2,345만원", 123_450_000),
    ],
)
def test_parse_exact(text, expected):
    assert parse_krw_text(text) == Exact(expected)


@pytest.mark.parametrize(
    ("text", "lo", "hi"),
    [
        ("3~4만원", 30_000, 40_000),
        ("3만~4만원", 30_000, 40_000),
        ("3만 ~ 4만원", 30_000, 40_000),
        ("5,000~6,000억", 5_000 * EOK, 6_000 * EOK),
        ("5천억~1조", 5_000 * EOK, JO),
    ],
)
def test_parse_range(text, lo, hi):
    assert parse_krw_text(text) == Range(lo, hi)


@pytest.mark.parametrize(
    "text",
    ["상승할 것", "", "억", "1억 5천", "4만~3만원", "1만~2만~3만", "만억", "1.2345원", "1억조", "3,50억"],
)
def test_parse_unparseable(text):
    assert isinstance(parse_krw_text(text), Unparseable)


def test_cross_check_arch08_cases():
    mismatch = cross_check("3,500억", 35_000_000_000)
    assert mismatch.status == "mismatch" and mismatch.blocks_confirm
    match = cross_check("3,500억", 350_000_000_000)
    assert match.status == "match" and not match.blocks_confirm


def test_cross_check_range():
    assert cross_check("3~4만원", 35_000).status == "match"
    assert cross_check("3~4만원", 30_000).status == "match"
    out = cross_check("3~4만원", 45_000)
    assert out.status == "mismatch" and out.blocks_confirm


def test_cross_check_unparseable_only_warns():
    out = cross_check("오를 만큼 오름", 50_000)
    assert out.status == "unparseable"
    assert not out.blocks_confirm


def test_range_invariant():
    with pytest.raises(ValueError):
        Range(2, 1)
