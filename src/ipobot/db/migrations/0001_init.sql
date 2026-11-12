-- 0001_init: 초기 스키마(계획 §6 + Part A auth_delay 컬럼)
CREATE TABLE ipo_listings (
  id INTEGER PRIMARY KEY,
  corp_code TEXT NOT NULL,
  corp_name TEXT NOT NULL,
  stock_code TEXT CHECK (stock_code IS NULL OR length(stock_code) = 6),
  market TEXT CHECK (market IS NULL OR market IN ('KOSPI','KOSDAQ','KONEX')),
  listing_date TEXT NOT NULL,              -- YYYY-MM-DD
  source_rcept_no TEXT NOT NULL,
  collected_at TEXT NOT NULL,
  UNIQUE (corp_code, listing_date)
);

CREATE TABLE stock_master (
  stock_code TEXT PRIMARY KEY CHECK (length(stock_code) = 6),
  corp_code TEXT,
  corp_name TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE summaries (
  source_rcept_no TEXT PRIMARY KEY,
  corp_code TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('ok','failed')),
  summary TEXT,
  retry_after TEXT,
  created_at TEXT NOT NULL,
  CHECK (status <> 'ok' OR summary IS NOT NULL),
  CHECK (status <> 'failed' OR retry_after IS NOT NULL)
);

-- 공개(비어있지 않은) 시장-일자만 저장한다. 빈 응답은 저장하지 않는다(negative는 메모리 캐시).
-- 이상 행(data_anomaly)이 하나라도 있는 시장-일자도 저장하지 않는다(다음 시도에서 재조회).
CREATE TABLE krx_market_days (
  trade_date TEXT NOT NULL,
  market TEXT NOT NULL CHECK (market IN ('KOSPI','KOSDAQ','KONEX')),
  row_count INTEGER NOT NULL CHECK (row_count > 0),
  fetched_at TEXT NOT NULL,
  PRIMARY KEY (trade_date, market)
);

CREATE TABLE krx_daily_quotes (
  trade_date TEXT NOT NULL,
  market TEXT NOT NULL,
  stock_code TEXT NOT NULL CHECK (length(stock_code) = 6),
  corp_name TEXT NOT NULL,                 -- ISU_NM
  close_price INTEGER NOT NULL CHECK (close_price >= 0),    -- TDD_CLSPRC
  open_price INTEGER NOT NULL CHECK (open_price >= 0),      -- TDD_OPNPRC
  trade_volume INTEGER NOT NULL CHECK (trade_volume >= 0),  -- ACC_TRDVOL
  market_cap INTEGER NOT NULL CHECK (market_cap >= 0),      -- MKTCAP
  listed_shares INTEGER NOT NULL CHECK (listed_shares >= 0),-- LIST_SHRS
  PRIMARY KEY (trade_date, market, stock_code),
  FOREIGN KEY (trade_date, market) REFERENCES krx_market_days(trade_date, market)
);

-- KRX 관측으로 확정된 휴장(세 시장 모두 빈 응답 + 이후 날짜 공개 + 기록 직전 캐시 우회 재조회에서도 빈 응답).
-- 캘린더 계층의 하나다. 오탐 정정은 market_calendar_overrides(is_open=1)로 한다(README).
CREATE TABLE krx_closed_days (
  trade_date TEXT PRIMARY KEY,
  evidence_date TEXT NOT NULL CHECK (evidence_date > trade_date),
  detected_at TEXT NOT NULL
);

CREATE TABLE market_calendar_overrides (
  day TEXT PRIMARY KEY,
  is_open INTEGER NOT NULL CHECK (is_open IN (0,1)),
  note TEXT
);

CREATE TABLE predictions (
  id INTEGER PRIMARY KEY,
  discord_user_id TEXT NOT NULL,
  discord_user_name TEXT NOT NULL,
  stock_code TEXT NOT NULL CHECK (length(stock_code) = 6),
  corp_name TEXT NOT NULL,
  market TEXT NOT NULL CHECK (market IN ('KOSPI','KOSDAQ','KONEX')),  -- 기준일 스냅샷의 시장(판정 힌트)
  base_date TEXT NOT NULL,
  base_price INTEGER NOT NULL CHECK (base_price > 0),
  base_market_cap INTEGER NOT NULL CHECK (base_market_cap > 0),
  reason TEXT NOT NULL,
  raw_text TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (discord_user_id, stock_code, base_date)
);

CREATE TABLE prediction_targets (
  prediction_id INTEGER NOT NULL REFERENCES predictions(id),
  horizon_months INTEGER NOT NULL CHECK (horizon_months IN (1,3,6,12)),
  target_date TEXT NOT NULL,
  pred_price INTEGER NOT NULL CHECK (pred_price > 0),
  pred_market_cap INTEGER NOT NULL CHECK (pred_market_cap > 0),
  price_text TEXT NOT NULL,
  market_cap_text TEXT NOT NULL,
  PRIMARY KEY (prediction_id, horizon_months)
);

CREATE TABLE judgments (
  id INTEGER PRIMARY KEY,
  stock_code TEXT NOT NULL,
  market TEXT NOT NULL CHECK (market IN ('KOSPI','KOSDAQ','KONEX')),
  base_date TEXT NOT NULL,
  horizon_months INTEGER NOT NULL CHECK (horizon_months IN (1,3,6,12)),
  target_date TEXT NOT NULL,
  judge_trade_date TEXT NOT NULL,          -- 현재 후보 = 직전 프런티어(표시용, streak 리셋 기준). 매 시도마다 갱신한다
  status TEXT NOT NULL CHECK (status IN ('pending','done','unjudgeable')),
  not_traded_days INTEGER NOT NULL DEFAULT 0 CHECK (not_traded_days >= 0),  -- 정책 순회 결과 캐시
  unavailable_since TEXT,                  -- 관측 미가용 연속 구간의 첫 계수 영업일(YYYY-MM-DD)
  unavailable_last_day TEXT,               -- 마지막으로 계수한 영업일(같은 날 중복 계수 방지)
  unavailable_days INTEGER NOT NULL DEFAULT 0 CHECK (unavailable_days >= 0),  -- 계수된 서로 다른 영업일 수
  next_attempt_at TEXT NOT NULL,
  actual_trade_date TEXT,
  actual_price INTEGER,
  actual_market_cap INTEGER,
  last_error TEXT,                         -- 판정 사유/관측 코드(전송 실패는 기록하지 않음)
  announce_parts_sent INTEGER NOT NULL DEFAULT 0 CHECK (announce_parts_sent >= 0),
  announce_message_id TEXT,                -- 첫 파트 message_id(NotFound 간주 시 'deleted')
  announce_error TEXT,                     -- 마지막 전송 실패 코드
  announced_at TEXT,                       -- 전 파트 성공 시각
  auth_delay_notified_at TEXT,             -- KRX 인증 오류 지연 안내 전 파트 성공 시각(ISO-8601 UTC). 상태 전이 후에도 보존(감사용)
  auth_delay_parts_sent INTEGER NOT NULL DEFAULT 0 CHECK (auth_delay_parts_sent >= 0),  -- 지연 안내 진행 카운터
  updated_at TEXT NOT NULL,
  UNIQUE (stock_code, base_date, horizon_months),
  CHECK (status <> 'done' OR (actual_trade_date IS NOT NULL AND actual_trade_date >= target_date
                               AND actual_price IS NOT NULL AND actual_price > 0
                               AND actual_market_cap IS NOT NULL AND actual_market_cap > 0)),
  CHECK (status = 'done' OR (actual_trade_date IS NULL AND actual_price IS NULL AND actual_market_cap IS NULL)),
  CHECK (status <> 'unjudgeable' OR last_error IS NOT NULL),
  CHECK ((unavailable_days = 0) = (unavailable_since IS NULL)),
  CHECK ((unavailable_since IS NULL) = (unavailable_last_day IS NULL)),
  CHECK (unavailable_last_day IS NULL OR unavailable_last_day >= unavailable_since),
  CHECK (announce_parts_sent = 0 OR status IN ('done','unjudgeable')),
  CHECK ((announce_parts_sent = 0) = (announce_message_id IS NULL)),
  CHECK (announced_at IS NULL OR status IN ('done','unjudgeable')),
  CHECK (announced_at IS NULL OR announce_parts_sent >= 1)
);

CREATE TABLE judgment_results (
  judgment_id INTEGER NOT NULL REFERENCES judgments(id),
  prediction_id INTEGER NOT NULL REFERENCES predictions(id),
  price_err_num INTEGER NOT NULL CHECK (price_err_num >= 0),
  price_err_den INTEGER NOT NULL CHECK (price_err_den > 0),
  mcap_err_num INTEGER NOT NULL CHECK (mcap_err_num >= 0),
  mcap_err_den INTEGER NOT NULL CHECK (mcap_err_den > 0),
  rank INTEGER NOT NULL CHECK (rank >= 1),
  is_solo INTEGER NOT NULL CHECK (is_solo IN (0,1)),
  PRIMARY KEY (judgment_id, prediction_id)
);

CREATE TABLE job_runs (
  job_key TEXT PRIMARY KEY,                -- 'weekly:2026-W05'
  status TEXT NOT NULL CHECK (status IN ('done','failed')),
  attempts INTEGER NOT NULL CHECK (attempts >= 1),
  ran_at TEXT NOT NULL,
  last_error TEXT,
  message_id TEXT,                         -- 첫 파트 message_id
  parts_sent INTEGER NOT NULL DEFAULT 0 CHECK (parts_sent >= 0),
  payload_json TEXT,                       -- 수집 성공 후 렌더링한 파트 목록(재시도 간 내용·분할 고정)
  CHECK (status <> 'done' OR (message_id IS NOT NULL AND parts_sent >= 1 AND payload_json IS NOT NULL)),
  CHECK (status <> 'failed' OR last_error IS NOT NULL),
  CHECK ((parts_sent = 0) = (message_id IS NULL)),
  CHECK (parts_sent = 0 OR payload_json IS NOT NULL)
);

PRAGMA user_version = 1;
