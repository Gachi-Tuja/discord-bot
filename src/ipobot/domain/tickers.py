"""종목코드 정규화(S0a 잠정 규칙): 6자리 [0-9A-Z]{6}, 'A' 접두사 제거, ISIN(KR7…)은 4~9번째 문자."""

from __future__ import annotations

import re

_CODE_RE = re.compile(r"^[0-9A-Z]{6}$")
_ISIN_RE = re.compile(r"^KR[0-9A-Z]{10}$")


def is_valid_code(code: str) -> bool:
    return bool(_CODE_RE.match(code))


def normalize_code(raw: str) -> str:
    """입력 코드를 6자리 종목코드로 바꾼다. 해석할 수 없으면 ValueError."""
    if not isinstance(raw, str):
        raise ValueError(f"종목코드는 문자열이어야 합니다: {raw!r}")
    text = raw.strip().upper()
    if _ISIN_RE.match(text):
        if not text.startswith("KR7"):
            raise ValueError(f"주식 ISIN(KR7…)이 아닙니다: {raw!r}")
        text = text[3:9]
    elif len(text) == 7 and text.startswith("A"):
        text = text[1:]
    if not _CODE_RE.match(text):
        raise ValueError(f"잘못된 종목코드: {raw!r}")
    return text
