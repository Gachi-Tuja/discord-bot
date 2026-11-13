"""S14: 포맷팅(25필드·6000자·2000자 분할, 결과·판정 불가·다이제스트·지연 안내·경고 문구)."""

from __future__ import annotations

from datetime import date

import discord
import pytest

from ipobot.bot.formatting import (
    NO_UPCOMING_IPOS,
    Part,
    amount_mismatch_warning,
    clip_content,
    amount_unparseable_warning,
    auth_delay_parts,
    digest_parts,
    embed_length,
    format_krw,
    format_percent,
    implied_shares_warning_text,
    parts_from_json,
    parts_to_json,
    result_parts,
    split_text,
)


def judgment(**over):
    row = {
        "id": 7,
        "stock_code": "123456",
        "market": "KOSDAQ",
        "base_date": "2026-11-02",
        "horizon_months": 1,
        "target_date": "2026-12-02",
        "judge_trade_date": "2026-12-02",
        "status": "done",
        "actual_trade_date": "2026-12-02",
        "actual_price": 10_000,
        "actual_market_cap": 100_000_000_000,
        "last_error": None,
        "unavailable_since": None,
        "unavailable_days": 0,
    }
    row.update(over)
    return row


def pred(pid, name, price=11_000, mcap=110_000_000_000):
    return {"id": pid, "discord_user_name": name, "corp_name": "가나다", "pred_price": price,
            "pred_market_cap": mcap}


def res(pid, rank, p=(1, 10), m=(1, 10), solo=0):
    return {"prediction_id": pid, "rank": rank, "price_err_num": p[0], "price_err_den": p[1],
            "mcap_err_num": m[0], "mcap_err_den": m[1], "is_solo": solo}


def only_embed(part: Part) -> dict:
    assert len(part.embeds) == 1
    return part.embeds[0]


def assert_within_limits(parts):
    for part in parts:
        for e in part.embeds:
            assert len(e.get("fields", ())) <= 25
            assert embed_length(e) <= 6000
            assert embed_length(e) == len(discord.Embed.from_dict(e))
        if part.content:
            assert len(part.content) <= 2000


def test_format_helpers():
    assert format_percent(1, 8) == "12.50%"
    assert format_percent(1, 3) == "33.33%"
    assert format_percent(2, 3) == "66.67%"
    assert format_percent(1, 200_000) == "0.00%"  # 0.0005% → 0.00
    assert format_percent(1, 20_000) == "0.01%"  # 0.005% half-up → 0.01
    assert format_percent(0, 5) == "0.00%"
    assert format_percent(25, 1) == "2,500.00%"
    assert format_krw(123_456_000_000) == "1,234억 5,600만원"
    assert format_krw(1_200_000_000_000) == "1조 2,000억원"
    assert format_krw(5_000) == "5,000원"
    assert format_krw(0) == "0원"


def test_result_group_with_joint_winners_and_r19_text():
    parts = result_parts(
        judgment(),
        [res(2, 1, (1, 20)), res(1, 1, (1, 20)), res(3, 3, (1, 4), (1, 2))],
        [pred(1, "철수"), pred(2, "영희"), pred(3, "민수")],
    )
    assert len(parts) == 1
    e = only_embed(parts[0])
    assert e["title"] == "📊 가나다(123456) 1개월 예측 결과"
    assert "판정 거래일 2026-12-02 종가 기준(비수정 종가)" in e["description"]
    assert "휴장·무거래" not in e["description"]
    assert "🏆 공동 1등: 철수, 영희" in e["description"]
    names = [f["name"] for f in e["fields"]]
    assert names == ["🥇 1위 · 철수", "🥇 1위 · 영희", "3위 · 민수"]
    assert e["fields"][2]["value"] == "주가 11,000원 → 오차 25.00%\n시총 1,100억원 → 오차 50.00%"
    assert_within_limits(parts)


def test_single_winner_label():
    e = only_embed(result_parts(judgment(), [res(1, 1), res(2, 2)], [pred(1, "철수"), pred(2, "영희")])[0])
    assert "🏆 1등: 철수" in e["description"]


def test_target_differs_from_trade_day_notice():
    e = only_embed(result_parts(judgment(actual_trade_date="2026-12-03"), [res(1, 1), res(2, 2)],
                                [pred(1, "a"), pred(2, "b")])[0])
    assert "판정 거래일 2026-12-03 종가 기준(비수정 종가)" in e["description"]
    assert "목표일 2026-12-02 휴장·무거래로 다음 거래일 적용" in e["description"]


def test_solo_shows_error_rates_only():
    e = only_embed(result_parts(judgment(), [res(1, 1, (1, 8), solo=1)], [pred(1, "철수")])[0])
    assert "단독 참가" in e["description"]
    assert "1등" not in e["description"]
    assert e["fields"][0]["name"] == "철수"
    assert "오차 12.50%" in e["fields"][0]["value"]


@pytest.mark.parametrize(("reason", "text"), [
    ("halted_suspected", "판정 불가: 거래정지 또는 무거래 추정"),
    ("delisted_suspected", "판정 불가: 상장폐지 추정"),
])
def test_unjudgeable_reasons(reason, text):
    j = judgment(status="unjudgeable", actual_trade_date=None, actual_price=None, actual_market_cap=None,
                 last_error=reason)
    e = only_embed(result_parts(j, [], [pred(2, "영희"), pred(1, "철수")])[0])
    assert e["title"] == "⚠️ 가나다(123456) 1개월 예측 판정 불가"
    assert text in e["description"]
    assert [f["name"] for f in e["fields"]] == ["철수", "영희"]


def test_unjudgeable_krx_unavailable_shows_since_and_days():
    j = judgment(status="unjudgeable", actual_trade_date=None, actual_price=None, actual_market_cap=None,
                 last_error="krx_unavailable", unavailable_since="2026-12-03", unavailable_days=10)
    e = only_embed(result_parts(j, [], [pred(1, "철수")])[0])
    assert "판정 불가: KRX 시세를 장기간 조회할 수 없음(2026-12-03부터 10영업일)" in e["description"]


def test_unknown_reason_and_pending_rejected():
    with pytest.raises(ValueError):
        result_parts(judgment(status="unjudgeable", last_error="krx_auth"), [], [pred(1, "a")])
    with pytest.raises(ValueError):
        result_parts(judgment(status="pending"), [], [pred(1, "a")])


def test_result_with_many_participants_splits_at_25_fields_deterministically():
    n = 30
    preds = [pred(i, f"참가자{i}") for i in range(1, n + 1)]
    results = [res(i, i) for i in range(1, n + 1)]
    parts = result_parts(judgment(), results, preds)
    assert len(parts) == 2
    first, second = only_embed(parts[0]), only_embed(parts[1])
    assert len(first["fields"]) == 25 and len(second["fields"]) == 5
    assert second["title"].endswith("(계속)")
    assert "description" not in second
    assert_within_limits(parts)
    assert result_parts(judgment(), list(reversed(results)), list(reversed(preds))) == parts


def digest_item(i, summary="한 줄 요약", day="2026-11-20", market="KOSDAQ"):
    return {"corp_name": f"종목{i:02d}", "listing_date": day, "market": market, "summary": summary}


def test_digest_empty_is_single_notice_part():
    parts = digest_parts([], date(2026, 11, 13))
    assert parts == [Part(content=NO_UPCOMING_IPOS)]


def test_digest_fields_show_name_date_market_summary_only():
    parts = digest_parts(
        [digest_item(2, day="2026-11-21"), digest_item(1, summary=None, market=None)],
        date(2026, 11, 13),
    )
    e = only_embed(parts[0])
    assert e["title"] == "향후 2주간 상장 예정 종목 (2026-11-13 기준)"
    assert e["fields"] == [
        {"name": "종목01 · 2026-11-20 · 시장 미정", "value": "요약 없음", "inline": False},
        {"name": "종목02 · 2026-11-21 · KOSDAQ", "value": "한 줄 요약", "inline": False},
    ]


def test_digest_over_25_fields_two_parts():
    items = [digest_item(i) for i in range(26)]
    parts = digest_parts(items, date(2026, 11, 13))
    assert [len(only_embed(p)["fields"]) for p in parts] == [25, 1]
    assert_within_limits(parts)


def test_digest_6000_char_split():
    items = [digest_item(i, summary="가" * 1000) for i in range(10)]
    parts = digest_parts(items, date(2026, 11, 13))
    assert len(parts) == 2
    assert [len(only_embed(p)["fields"]) for p in parts] == [5, 5]
    assert_within_limits(parts)
    assert digest_parts(list(reversed(items)), date(2026, 11, 13)) == parts


def test_long_field_value_is_truncated():
    parts = digest_parts([digest_item(1, summary="나" * 3000)], date(2026, 11, 13))
    value = only_embed(parts[0])["fields"][0]["value"]
    assert len(value) == 1024 and value.endswith("…")


def test_payload_json_round_trip_and_kwargs():
    parts = digest_parts([digest_item(i) for i in range(26)], date(2026, 11, 13)) + [Part(content="x")]
    restored = parts_from_json(parts_to_json(parts))
    assert restored == parts
    kw = restored[0].kwargs
    assert set(kw) == {"embeds"}
    assert isinstance(kw["embeds"][0], discord.Embed)
    assert kw["embeds"][0].title == "향후 2주간 상장 예정 종목 (2026-11-13 기준)"
    assert restored[-1].kwargs == {"content": "x"}


def test_auth_delay_notice_text():
    parts = auth_delay_parts("가나다")
    assert parts == [Part(content="⏳ 가나다 예측 결과: 데이터 출처 인증 오류로 결과 발표가 지연되고 있습니다. "
                                  "복구되면 결과를 공지합니다.")]


def test_split_text_2000_limit():
    assert split_text("abc") == ["abc"]
    text = "\n".join(["a" * 1500, "b" * 600, "c" * 10])
    assert split_text(text) == ["a" * 1500, "b" * 600 + "\n" + "c" * 10]
    long = split_text("x" * 4500)
    assert [len(c) for c in long] == [2000, 2000, 500]
    assert all(len(c) <= 2000 for c in split_text(("y" * 700 + "\n") * 10))


def test_empty_part_rejected():
    with pytest.raises(ValueError):
        Part()
    with pytest.raises(ValueError):
        Part(content="z" * 2001)


def test_warning_texts():
    assert amount_mismatch_warning("1조2천억", 120_000_000_000) == "금액 해석 불일치: 텍스트 1조2천억 ↔ 해석 1,200억원"
    assert "확정은 가능" in amount_unparseable_warning("대략 많이")
    w = implied_shares_warning_text(10_000, 1_000_000_000_000, 1_000_000)
    assert "1조원" in w and "100억원" in w and "10배" in w


def test_amount_warnings_truncate_echoed_text():
    long = "3" * 5000
    mismatch = amount_mismatch_warning(long, 30_000)
    unparseable = amount_unparseable_warning("  " + "대략" * 2500 + "  ")
    assert "3" * 40 + "…" in mismatch and "3" * 41 not in mismatch
    assert len(mismatch) < 100 and len(unparseable) < 100
    assert amount_mismatch_warning(" 3만원 ", 30_000) == "금액 해석 불일치: 텍스트 3만원 ↔ 해석 3만원"


def test_clip_content_bounds_to_2000():
    assert clip_content("가" * 2000) == "가" * 2000
    clipped = clip_content("가" * 5000)
    assert len(clipped) == 2000 and clipped.endswith("…")
