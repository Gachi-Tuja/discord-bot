"""예측 초안·확정(S11, R1·R2·R3·R5~R8·R12·R20 3단계).

흐름
- `draft`: 파싱(S10) → 종목 해석(R7) → 기준일·기준값(R1·R2) → 목표일(R3) → 사전 검사(R5·R12) → 상식 경고(R20).
- `confirm_draft`: R7·R1을 negative 캐시를 우회해 다시 확인한다(R6, F9). 기준일이 바뀌었으면 거부하고,
  아니면 `PredictionRepo.confirm` 트랜잭션 1개로 저장한다(R8). 중복·그룹 판정됨은 그 트랜잭션이 최종 판단한다.

거부는 예외가 아니라 `Rejected(message)`로 돌려준다. `LlmError`만 예외로 올라간다(파서와 같은 규칙).
서비스는 트랜잭션 사이에서만 await한다(R24).
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, time
from typing import Protocol

from ipobot.bot.formatting import (
    REJECT_BASE_DATE_CHANGED,
    REJECT_DUPLICATE,
    REJECT_GROUP_CLOSED,
    REJECT_KRX_FAILED,
    REJECT_KRX_UNPUBLISHED,
    REJECT_NO_BASE_PRICE,
    REJECT_STOCK_AMBIGUOUS,
    REJECT_STOCK_CHANGED,
    REJECT_STOCK_NOT_FOUND,
    implied_shares_warning_text,
)
from ipobot.clock import Clock, today_kst
from ipobot.db.repositories import (
    DuplicatePredictionError,
    GroupClosedError,
    IpoListingRepo,
    NewJudgment,
    NewPrediction,
    PredictionRepo,
    StockMasterRepo,
)
from ipobot.domain.calendar import TradingCalendar
from ipobot.domain.dates import attempt_morning, base_candidates, first_open_on_or_after, next_open, target_dates
from ipobot.domain.models import (
    AllEmpty,
    DailyQuote,
    DayObs,
    FetchError,
    Market,
    NotTraded,
    ParsedPrediction,
    PredictionTarget,
    Traded,
    Unpublished,
)
from ipobot.domain.sanity import implied_shares_warning
from ipobot.domain.tickers import normalize_code
from ipobot.services.alerts import operator_alert
from ipobot.services.prediction_parser import ParseRejection, PredictionParser

log = logging.getLogger(__name__)

# `동원파츠(A0162D0)`처럼 이름 뒤 괄호에 코드를 함께 쓴 입력.
_BRACKETED_CODE_RE = re.compile(r"^.*?[(\[]\s*([^()\[\]]+?)\s*[)\]]$")


class QuotePort(Protocol):
    async def day_obs(
        self, code: str, trade_date: date, hint: Market | None, bypass_negative_cache: bool = False
    ) -> DayObs: ...

    async def find_by_name(
        self, corp_name: str, trade_date: date, bypass_negative_cache: bool = False
    ) -> list[DailyQuote] | None | FetchError: ...


@dataclass(frozen=True)
class Rejected:
    message: str


@dataclass(frozen=True)
class Draft:
    discord_user_id: str
    discord_user_name: str
    raw_text: str
    parsed: ParsedPrediction
    base: DailyQuote  # 기준일 관측(R1·R2): trade_date·market·close_price·market_cap·listed_shares
    targets: tuple[tuple[PredictionTarget, date], ...]  # (예측, 목표일), 1·3·6·12 순서
    warnings: tuple[str, ...]

    @property
    def stock_code(self) -> str:
        return self.base.stock_code

    @property
    def base_date(self) -> date:
        return self.base.trade_date


@dataclass(frozen=True)
class Confirmed:
    prediction_id: int
    draft: Draft


class PredictionService:
    def __init__(
        self,
        parser: PredictionParser,
        quotes: QuotePort,
        conn: sqlite3.Connection,
        clock: Clock,
        cal: TradingCalendar,
        *,
        lookback: int,
        judge_time: time,
    ) -> None:
        self._parser = parser
        self._quotes = quotes
        self._predictions = PredictionRepo(conn)
        self._master = StockMasterRepo(conn)
        self._listings = IpoListingRepo(conn)
        self._clock = clock
        self._cal = cal
        self._lookback = lookback
        self._judge_time = judge_time

    async def draft(self, discord_user_id: str, discord_user_name: str, text: str) -> Draft | Rejected:
        parsed = await self._parser.parse(text)
        if isinstance(parsed, ParseRejection):
            return Rejected(parsed.message)
        base = await self._observe(parsed.stock_query, bypass=False)
        if isinstance(base, Rejected):
            return base
        if self._predictions.exists(discord_user_id, base.stock_code, base.trade_date):
            return Rejected(REJECT_DUPLICATE)
        if self._predictions.group_closed(base.stock_code, base.trade_date):
            return Rejected(REJECT_GROUP_CLOSED)
        dates = target_dates(base.trade_date)
        warnings = list(parsed.warnings)
        for t in parsed.targets:
            if implied_shares_warning(t.pred_price, t.pred_market_cap, base.listed_shares):
                warnings.append(implied_shares_warning_text(t.pred_price, t.pred_market_cap, base.listed_shares))
        return Draft(
            discord_user_id=discord_user_id,
            discord_user_name=discord_user_name,
            raw_text=text,
            parsed=parsed,
            base=base,
            targets=tuple((t, dates[t.horizon_months]) for t in parsed.targets),
            warnings=tuple(warnings),
        )

    async def confirm_draft(self, draft: Draft) -> Confirmed | Rejected:
        base = await self._observe(draft.parsed.stock_query, bypass=True, expected_code=draft.stock_code)
        if isinstance(base, Rejected):
            return base
        if base.trade_date != draft.base_date:
            log.info("기준일 변경 code=%s draft=%s now=%s", base.stock_code, draft.base_date, base.trade_date)
            return Rejected(REJECT_BASE_DATE_CHANGED)
        prediction = NewPrediction(
            discord_user_id=draft.discord_user_id,
            discord_user_name=draft.discord_user_name,
            stock_code=base.stock_code,
            corp_name=base.corp_name,
            market=base.market,
            base_date=base.trade_date,
            base_price=base.close_price,
            base_market_cap=base.market_cap,
            reason=draft.parsed.reason,
            raw_text=draft.raw_text,
            targets=draft.targets,
        )
        judgments = [self._new_judgment(base, t.horizon_months, target) for t, target in draft.targets]
        try:
            prediction_id = self._predictions.confirm(prediction, judgments, self._clock.now())
        except DuplicatePredictionError:
            return Rejected(REJECT_DUPLICATE)
        except GroupClosedError as exc:
            log.info("그룹 판정됨으로 거부: %s", exc)
            return Rejected(REJECT_GROUP_CLOSED)
        return Confirmed(prediction_id, draft)

    def _new_judgment(self, base: DailyQuote, horizon: int, target: date) -> NewJudgment:
        """R8 3단계 초기값."""
        judge_day = first_open_on_or_after(target, self._cal)
        return NewJudgment(
            stock_code=base.stock_code,
            market=base.market,
            base_date=base.trade_date,
            horizon_months=horizon,
            target_date=target,
            judge_trade_date=judge_day,
            next_attempt_at=attempt_morning(next_open(judge_day, self._cal), self._judge_time),
        )

    # --- R7 + R1 ---

    async def _observe(
        self, query: str, *, bypass: bool, expected_code: str | None = None
    ) -> DailyQuote | Rejected:
        today = today_kst(self._clock)
        candidates = base_candidates(today, self._cal, self._lookback)
        code = await self._resolve(query, candidates, bypass)
        if isinstance(code, Rejected):
            return code
        if expected_code is not None and code != expected_code:
            log.info("종목 해석 변경 query=%s draft=%s now=%s", query, expected_code, code)
            return Rejected(REJECT_STOCK_CHANGED)
        return await self._base_quote(code, candidates, bypass)

    async def _resolve(self, query: str, candidates: list[date], bypass: bool) -> str | Rejected:
        """R7: 코드 패턴(전체 또는 `이름(코드)` 괄호 안) → stock_master 이름 → lookback 안 최신 공개일 KRX 스냅샷 이름."""
        text = query.strip()
        bracketed = _BRACKETED_CODE_RE.match(text)
        for candidate in (text, bracketed.group(1) if bracketed else None):
            if candidate is None:
                continue
            try:
                return normalize_code(candidate)
            except ValueError:
                pass
        codes = self._master.codes_for_name(text)
        if len(codes) == 1:
            return codes[0]
        if len(codes) > 1:
            return Rejected(REJECT_STOCK_AMBIGUOUS)
        for d in candidates:
            found = await self._quotes.find_by_name(text, d, bypass)
            if isinstance(found, FetchError):
                return self._fetch_failed(found, f"name={text} date={d}")
            if found is None:
                continue
            codes = sorted({q.stock_code for q in found})
            if len(codes) == 1:
                return codes[0]
            return Rejected(REJECT_STOCK_AMBIGUOUS if codes else REJECT_STOCK_NOT_FOUND)
        return Rejected(REJECT_KRX_UNPUBLISHED)

    async def _base_quote(self, code: str, candidates: list[date], bypass: bool) -> DailyQuote | Rejected:
        """R1: 등록 시점에 KRX가 공개한 최신 거래일의 관측."""
        hint = self._listings.market_for_code(code)
        for d in candidates:
            obs = await self._quotes.day_obs(code, d, hint, bypass)
            if isinstance(obs, Traded):
                return obs.quote
            if isinstance(obs, NotTraded):
                return Rejected(REJECT_NO_BASE_PRICE)
            if isinstance(obs, FetchError):
                return self._fetch_failed(obs, f"code={code} date={d}")
            if isinstance(obs, (AllEmpty, Unpublished)):
                continue
            raise TypeError(f"알 수 없는 관측: {obs!r}")
        return Rejected(REJECT_KRX_UNPUBLISHED)

    @staticmethod
    def _fetch_failed(err: FetchError, detail: str) -> Rejected:
        if err.kind == "auth":
            operator_alert("krx_auth", detail)
        return Rejected(REJECT_KRX_FAILED)
