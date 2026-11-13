"""S4: 종목코드 정규화."""

from __future__ import annotations

import pytest

from ipobot.domain.tickers import is_valid_code, normalize_code


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("A0158S0", "0158S0"),
        ("0158S0", "0158S0"),
        ("005930", "005930"),
        ("A005930", "005930"),
        (" 005930 ", "005930"),
        ("a005930", "005930"),
        ("KR7005930003", "005930"),
        ("KR70158S0001", "0158S0"),
    ],
)
def test_normalize_code(raw, expected):
    assert normalize_code(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "12345", "1234567", "B005930", "00593!", "KR8005930003", "삼성전자", "KR700593000"],
)
def test_normalize_code_rejects(raw):
    with pytest.raises(ValueError):
        normalize_code(raw)


def test_is_valid_code():
    assert is_valid_code("0158S0")
    assert not is_valid_code("0158s0")
    assert not is_valid_code("A0158S0")
