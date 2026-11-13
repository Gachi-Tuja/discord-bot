"""디스코드 메시지 포맷팅(S14). 순수 함수: 같은 입력 → 같은 파트 분할.

입력은 DB 행과 같은 열 이름을 쓰는 매핑(`sqlite3.Row` 포함)이다. 파트 하나가 메시지 하나이며,
임베드는 25필드·6000자 한도 안에서 결정적으로 나눈다. 일반 텍스트는 2000자 단위로 나눈다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import discord

CONTENT_LIMIT = 2000
EMBED_TOTAL_LIMIT = 6000
EMBED_FIELDS_LIMIT = 25
EMBED_TITLE_LIMIT = 256
EMBED_DESCRIPTION_LIMIT = 4096
FIELD_NAME_LIMIT = 256
FIELD_VALUE_LIMIT = 1024
ELLIPSIS = "…"
# 거부·경고 문구에 그대로 옮기는 사용자 금액 텍스트의 최대 길이(넘으면 잘라 「…」를 붙인다).
AMOUNT_ECHO_LIMIT = 40

NO_UPCOMING_IPOS = "향후 2주간 상장 예정 종목이 없습니다"
DIGEST_TITLE = "향후 2주간 상장 예정 종목"
NO_SUMMARY = "요약 없음"
CONFIRM_RESULT_SCHEDULE = "결과는 판정 거래일 다음 영업일 오전, KRX 공개 후 공지됩니다"

# 예측 등록 거부 문구(S11, R1·R5·R6·R7·R12). 사유마다 문구가 고유하다.
REJECT_STOCK_NOT_FOUND = "종목을 찾을 수 없습니다"
REJECT_STOCK_AMBIGUOUS = "같은 이름의 종목이 여러 개입니다. 종목코드로 다시 입력해 주세요"
REJECT_KRX_UNPUBLISHED = "KRX 시세가 아직 공개되지 않았습니다. 잠시 후 다시 제출해 주세요"
REJECT_KRX_FAILED = "KRX 조회에 실패했습니다. 잠시 후 다시 시도해 주세요"
REJECT_NO_BASE_PRICE = "기준 종가를 조회할 수 없습니다(거래정지 또는 상장 전)"
REJECT_BASE_DATE_CHANGED = "기준일이 바뀌었습니다. 다시 제출해 주세요"
REJECT_STOCK_CHANGED = "종목 해석이 바뀌었습니다. 다시 제출해 주세요"
REJECT_DUPLICATE = "이미 제출한 예측입니다(같은 종목·기준일)"
REJECT_GROUP_CLOSED = "이미 판정된 그룹입니다"

UNJUDGEABLE_REASON_TEXT = {
    "halted_suspected": "거래정지 또는 무거래 추정",
    "delisted_suspected": "상장폐지 추정",
    "krx_unavailable": "KRX 시세를 장기간 조회할 수 없음",
    "invalid_actual": "실제 시세가 0 이하로 조회되어 오차를 계산할 수 없음",
}

RESULT_COLOR = 0x2ECC71
UNJUDGEABLE_COLOR = 0x95A5A6
DIGEST_COLOR = 0x3498DB


@dataclass(frozen=True)
class Part:
    """메시지 1개. embeds는 JSON 호환 dict(`discord.Embed.to_dict` 형식)이다."""

    content: str | None = None
    embeds: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not self.content and not self.embeds:
            raise ValueError("빈 파트는 보낼 수 없습니다")
        if self.content is not None and len(self.content) > CONTENT_LIMIT:
            raise ValueError(f"content가 {CONTENT_LIMIT}자를 넘습니다")

    @property
    def kwargs(self) -> dict[str, Any]:
        kw: dict[str, Any] = {}
        if self.content:
            kw["content"] = self.content
        if self.embeds:
            kw["embeds"] = [discord.Embed.from_dict(e) for e in self.embeds]
        return kw

    def to_json_obj(self) -> dict[str, Any]:
        return {"content": self.content, "embeds": list(self.embeds)}

    @classmethod
    def from_json_obj(cls, obj: Mapping[str, Any]) -> Part:
        return cls(content=obj.get("content"), embeds=tuple(obj.get("embeds") or ()))


def parts_to_json(parts: Sequence[Part]) -> str:
    """job_runs.payload_json 저장용(재시도 간 내용·분할 고정)."""
    return json.dumps([p.to_json_obj() for p in parts], ensure_ascii=False, sort_keys=True)


def parts_from_json(payload: str) -> list[Part]:
    return [Part.from_json_obj(o) for o in json.loads(payload)]


# ---------------------------------------------------------------- 기본 서식


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - len(ELLIPSIS)] + ELLIPSIS


def clip_content(text: str, limit: int = CONTENT_LIMIT) -> str:
    """디스코드 메시지 한 건(기본 2000자)에 들어가도록 자른다. 넘으면 끝을 「…」로 바꾼다."""
    return _truncate(text, limit)


def _echo_amount(text: str) -> str:
    """금액 텍스트를 최대 AMOUNT_ECHO_LIMIT자 + 「…」로 되돌려 보인다."""
    return _truncate(text.strip(), AMOUNT_ECHO_LIMIT + len(ELLIPSIS))


def format_won(value: int) -> str:
    return f"{value:,}원"


def format_krw(value: int) -> str:
    """큰 금액을 조·억·만 단위로 쓴다. 예: 123_456_000_000 → '1,234억 5,600만원'."""
    if value < 0:
        raise ValueError("음수 금액")
    if value == 0:
        return "0원"
    jo, rest = divmod(value, 10**12)
    eok, rest = divmod(rest, 10**8)
    man, won = divmod(rest, 10**4)
    units = [(jo, "조"), (eok, "억"), (man, "만")]
    text = " ".join(f"{n:,}{u}" for n, u in units if n)
    if won:
        text = f"{text} {won:,}" if text else f"{won:,}"
    return text + "원"


def format_percent(num: int, den: int) -> str:
    """오차율 num/den을 백분율 소수 2자리로 반올림(half-up)한다. 예: 1/8 → '12.50%'."""
    if num < 0 or den <= 0:
        raise ValueError("오차율은 num ≥ 0, den > 0이어야 합니다")
    scaled = (2 * num * 10_000 + den) // (2 * den)  # round(num/den * 100 * 100)
    return f"{scaled // 100:,}.{scaled % 100:02d}%"


def _day(value: Any) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)


# ---------------------------------------------------------------- 분할


def split_text(text: str, limit: int = CONTENT_LIMIT) -> list[str]:
    """줄 경계 우선으로 limit자 이하 조각으로 나눈다. 한 줄이 limit을 넘으면 강제로 자른다."""
    if limit <= 0:
        raise ValueError("limit은 양수여야 합니다")
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if current and len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def text_parts(text: str) -> list[Part]:
    return [Part(content=c) for c in split_text(text)]


def embed_length(embed: Mapping[str, Any]) -> int:
    """Discord 6000자 한도 계산(제목, 설명, 필드 이름·값, 푸터, 작성자)."""
    total = len(embed.get("title", "")) + len(embed.get("description", ""))
    total += sum(len(f["name"]) + len(f["value"]) for f in embed.get("fields", ()))
    total += len(embed.get("footer", {}).get("text", ""))
    total += len(embed.get("author", {}).get("name", ""))
    return total


def _field(name: str, value: str) -> dict[str, Any]:
    return {
        "name": _truncate(name, FIELD_NAME_LIMIT) or "-",
        "value": _truncate(value, FIELD_VALUE_LIMIT) or "-",
        "inline": False,
    }


def split_embed(
    title: str,
    description: str,
    fields: Sequence[dict[str, Any]],
    color: int,
) -> list[Part]:
    """첫 임베드에 제목·설명, 이어지는 임베드에 '(계속)' 제목. 각 임베드 ≤25필드·≤6000자."""
    title = _truncate(title, EMBED_TITLE_LIMIT)
    cont_title = _truncate(f"{title} (계속)", EMBED_TITLE_LIMIT)
    description = _truncate(description, EMBED_DESCRIPTION_LIMIT)

    def new_embed(first: bool) -> dict[str, Any]:
        e: dict[str, Any] = {"title": title if first else cont_title, "color": color, "fields": []}
        if first and description:
            e["description"] = description
        return e

    embeds = [new_embed(True)]
    for f in fields:
        cur = embeds[-1]
        size = len(f["name"]) + len(f["value"])
        if len(cur["fields"]) >= EMBED_FIELDS_LIMIT or embed_length(cur) + size > EMBED_TOTAL_LIMIT:
            cur = new_embed(False)
            embeds.append(cur)
        cur["fields"].append(f)
    for e in embeds:
        if not e["fields"]:
            del e["fields"]
    return [Part(embeds=(e,)) for e in embeds]


# ---------------------------------------------------------------- 주간 다이제스트 (R18)


def digest_parts(items: Sequence[Mapping[str, Any]], post_date: date) -> list[Part]:
    """items: corp_name, listing_date, market(None 가능), summary(None이면 '요약 없음').

    수집 성공이 확인된 경우에만 호출한다. 0건이면 안내 1파트.
    """
    if not items:
        return [Part(content=NO_UPCOMING_IPOS)]
    ordered = sorted(items, key=lambda it: (_day(it["listing_date"]), it["corp_name"], it["market"] or ""))
    fields = [
        _field(
            f"{it['corp_name']} · {_day(it['listing_date'])} · {it['market'] or '시장 미정'}",
            (it["summary"] or "").strip() or NO_SUMMARY,
        )
        for it in ordered
    ]
    title = f"{DIGEST_TITLE} ({post_date.isoformat()} 기준)"
    return split_embed(title, f"총 {len(ordered)}개 종목", fields, DIGEST_COLOR)


# ---------------------------------------------------------------- 판정 결과 공지 (R19, R22)


def _ranked(results: Sequence[Mapping[str, Any]], predictions: Sequence[Mapping[str, Any]]):
    by_id = {p["id"]: p for p in predictions}
    rows = []
    for r in results:
        p = by_id.get(r["prediction_id"])
        if p is None:
            raise ValueError(f"결과의 예측 {r['prediction_id']}가 predictions에 없습니다")
        rows.append((r, p))
    rows.sort(key=lambda rp: (rp[0]["rank"], rp[0]["prediction_id"]))
    return rows


def _corp_name(predictions: Sequence[Mapping[str, Any]], judgment: Mapping[str, Any]) -> str:
    return predictions[0]["corp_name"] if predictions else judgment["stock_code"]


def result_parts(
    judgment: Mapping[str, Any],
    results: Sequence[Mapping[str, Any]],
    predictions: Sequence[Mapping[str, Any]],
) -> list[Part]:
    """판정 공지 파트.

    judgment: judgments 행. results: judgment_results 행(done일 때).
    predictions: id, discord_user_name, corp_name, pred_price, pred_market_cap
    (predictions + prediction_targets 해당 기간 조인).
    """
    status = judgment["status"]
    corp = _corp_name(predictions, judgment)
    head = f"{corp}({judgment['stock_code']}) {judgment['horizon_months']}개월 예측"
    if status == "done":
        return _done_parts(head, judgment, results, predictions)
    if status == "unjudgeable":
        return _unjudgeable_parts(head, judgment, predictions)
    raise ValueError(f"공지할 수 없는 상태: {status}")


def _done_parts(head, judgment, results, predictions) -> list[Part]:
    trade_day = _day(judgment["actual_trade_date"])
    target_day = _day(judgment["target_date"])
    lines = [
        f"기준일 {_day(judgment['base_date'])} · 목표일 {target_day}",
        f"판정 거래일 {trade_day} 종가 기준(비수정 종가)",
    ]
    if trade_day != target_day:
        lines.append(f"목표일 {target_day} 휴장·무거래로 다음 거래일 적용")
    lines.append(
        f"실제 종가 {format_won(judgment['actual_price'])} · 실제 시총 {format_krw(judgment['actual_market_cap'])}"
    )
    ranked = _ranked(results, predictions)
    solo = len(ranked) == 1 and bool(ranked[0][0]["is_solo"])
    if solo:
        lines.append("단독 참가로 순위 없이 오차율만 공개합니다.")
    else:
        winners = [p["discord_user_name"] for r, p in ranked if r["rank"] == 1]
        label = "공동 1등" if len(winners) > 1 else "1등"
        lines.append(f"🏆 {label}: {', '.join(winners)}")

    fields = []
    for r, p in ranked:
        name = p["discord_user_name"] if solo else f"{r['rank']}위 · {p['discord_user_name']}"
        if not solo and r["rank"] == 1:
            name = f"🥇 {name}"
        value = (
            f"주가 {format_won(p['pred_price'])} → 오차 {format_percent(r['price_err_num'], r['price_err_den'])}\n"
            f"시총 {format_krw(p['pred_market_cap'])} → 오차 {format_percent(r['mcap_err_num'], r['mcap_err_den'])}"
        )
        fields.append(_field(name, value))
    return split_embed(f"📊 {head} 결과", "\n".join(lines), fields, RESULT_COLOR)


def unjudgeable_reason_text(judgment: Mapping[str, Any]) -> str:
    reason = judgment["last_error"]
    text = UNJUDGEABLE_REASON_TEXT.get(reason)
    if text is None:
        raise ValueError(f"알 수 없는 판정 불가 사유: {reason}")
    if reason == "krx_unavailable" and judgment["unavailable_since"]:
        text += f"({_day(judgment['unavailable_since'])}부터 {judgment['unavailable_days']}영업일)"
    return text


def _unjudgeable_parts(head, judgment, predictions) -> list[Part]:
    lines = [
        f"기준일 {_day(judgment['base_date'])} · 목표일 {_day(judgment['target_date'])}",
        f"판정 불가: {unjudgeable_reason_text(judgment)}",
    ]
    ordered = sorted(predictions, key=lambda p: p["id"])
    fields = [
        _field(
            p["discord_user_name"],
            f"예측 주가 {format_won(p['pred_price'])} · 예측 시총 {format_krw(p['pred_market_cap'])}",
        )
        for p in ordered
    ]
    return split_embed(f"⚠️ {head} 판정 불가", "\n".join(lines), fields, UNJUDGEABLE_COLOR)


# ---------------------------------------------------------------- 인증 오류 지연 안내 (Part A1)


def auth_delay_text(corp_name: str) -> str:
    return (
        f"⏳ {corp_name} 예측 결과: 데이터 출처 인증 오류로 결과 발표가 지연되고 있습니다. "
        "복구되면 결과를 공지합니다."
    )


def auth_delay_parts(corp_name: str) -> list[Part]:
    return text_parts(auth_delay_text(corp_name))


# ---------------------------------------------------------------- 미리보기 경고 (R20)


def amount_mismatch_warning(text: str, interpreted: int) -> str:
    return f"금액 해석 불일치: 텍스트 {_echo_amount(text)} ↔ 해석 {format_krw(interpreted)}"


def amount_unparseable_warning(text: str) -> str:
    return f"⚠️ 금액 텍스트를 해석하지 못했습니다: {_echo_amount(text)} (확정은 가능)"


def implied_shares_warning_text(pred_price: int, pred_market_cap: int, base_listed_shares: int) -> str:
    implied = pred_price * base_listed_shares
    return (
        f"⚠️ 예측 시총 {format_krw(pred_market_cap)}이 예측 주가 × 상장주식수 "
        f"{format_krw(implied)}와 10배 이상 차이 납니다 (확정은 가능)"
    )
