"""한국어 금액 텍스트의 결정적 해석과 LLM 정수 교차검증(R20, ARCH-08).

지원: 원·십·백·천·만·억·조 단위, 콤마, 소수(예: 1.5조), 복합(1조 2,000억), 범위(3~4만원, 3만~4만원).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Literal


@dataclass(frozen=True)
class Exact:
    value: int


@dataclass(frozen=True)
class Range:
    lo: int
    hi: int

    def __post_init__(self) -> None:
        if self.lo > self.hi:
            raise ValueError(f"Range lo > hi: {self.lo} > {self.hi}")


@dataclass(frozen=True)
class Unparseable:
    text: str


ParsedAmount = Exact | Range | Unparseable

_BIG = {"조": 10**12, "억": 10**8, "만": 10**4}
_SMALL = {"천": 1000, "백": 100, "십": 10}
_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_TERM_RE = re.compile(rf"({_NUMBER})([천백십])?([조억만])?")
_RANGE_SPLIT_RE = re.compile(r"\s*[~～〜]\s*")
_PREFIXES = ("약",)
_SUFFIXES = ("원",)


@dataclass(frozen=True)
class _Single:
    value: Fraction
    has_unit: bool
    last_multiplier: int  # 마지막 항의 단위 배수(단위 없으면 1)


def _strip_decor(text: str) -> str:
    s = text.strip()
    for p in _PREFIXES:
        if s.startswith(p):
            s = s[len(p):].strip()
    for suf in _SUFFIXES:
        if s.endswith(suf):
            s = s[: -len(suf)].strip()
    return s


def _parse_single(text: str) -> _Single | None:
    s = re.sub(r"\s+", "", text)
    if not s:
        return None
    pos = 0
    total = Fraction(0)
    prev_big: int | None = None
    has_unit = False
    last_mult = 1
    ended_without_big = False
    while pos < len(s):
        m = _TERM_RE.match(s, pos)
        if m is None or m.end() == pos:
            return None
        if ended_without_big:
            return None  # 단위 없는 항 뒤에 다른 항이 올 수 없다
        number = Fraction(m.group(1).replace(",", ""))
        small = _SMALL[m.group(2)] if m.group(2) else 1
        big = _BIG[m.group(3)] if m.group(3) else 1
        if m.group(3):
            if prev_big is not None and big >= prev_big:
                return None  # 조 > 억 > 만 순서만 허용
            prev_big = big
        else:
            ended_without_big = True
            if prev_big is not None:
                return None  # '1억 5천'(1.5억? 1억5천원?) 같은 모호한 꼬리는 해석하지 않는다
        mult = small * big
        has_unit = has_unit or mult > 1
        last_mult = mult
        total += number * mult
        pos = m.end()
    return _Single(total, has_unit, last_mult)


def _to_int(value: Fraction) -> int | None:
    return value.numerator if value.denominator == 1 else None


def parse_krw_text(text: str) -> ParsedAmount:
    """금액 텍스트를 Exact/Range/Unparseable로 해석한다."""
    if not isinstance(text, str):
        return Unparseable(str(text))
    body = _strip_decor(text)
    pieces = _RANGE_SPLIT_RE.split(body)
    if len(pieces) == 1:
        single = _parse_single(_strip_decor(pieces[0]))
        if single is None:
            return Unparseable(text)
        value = _to_int(single.value)
        return Exact(value) if value is not None else Unparseable(text)
    if len(pieces) != 2:
        return Unparseable(text)
    left = _parse_single(_strip_decor(pieces[0]))
    right = _parse_single(_strip_decor(pieces[1]))
    if left is None or right is None:
        return Unparseable(text)
    lo_value = left.value if left.has_unit else left.value * right.last_multiplier
    lo, hi = _to_int(lo_value), _to_int(right.value)
    if lo is None or hi is None or lo > hi:
        return Unparseable(text)
    return Range(lo, hi)


CrossCheckStatus = Literal["match", "mismatch", "unparseable"]


@dataclass(frozen=True)
class CrossCheck:
    status: CrossCheckStatus
    parsed: ParsedAmount

    @property
    def blocks_confirm(self) -> bool:
        return self.status == "mismatch"


def cross_check(text: str, llm_value: int) -> CrossCheck:
    """텍스트 해석과 LLM 정수를 비교한다. 불일치는 확정 차단, 해석 불가는 경고만."""
    parsed = parse_krw_text(text)
    if isinstance(parsed, Unparseable):
        return CrossCheck("unparseable", parsed)
    if isinstance(parsed, Exact):
        ok = parsed.value == llm_value
    else:
        ok = parsed.lo <= llm_value <= parsed.hi
    return CrossCheck("match" if ok else "mismatch", parsed)
