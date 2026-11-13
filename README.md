# ipobot — 공모주 상장 알림·주가 예측 디스코드 봇

이 봇은 세 가지 일을 한다.

- 매주 정해진 시각에 향후 2주간의 상장 예정 종목을 요약해 게시한다(DART 원문 기반).
- `/predict`로 상장 종목의 1·3·6·12개월 뒤 주가·시가총액 예측을 받는다.
- 목표일이 지나면 KRX 일별매매정보 종가로 판정해 결과를 공지한다.

> **현재 상태: 테스트 fixture는 잠정(합성) 데이터다.**
> `tests/fixtures/*.json`에는 모두 `"_provisional": true`가 붙어 있다. 실제 KRX 키로 응답을 기록하는 S0b(CP-KRX)를 아직 실행하지 못했기 때문이다.
> 아래 「S0b: 실제 KRX fixture 교체 절차」를 마치기 전에는 수동 스모크와 릴리스를 진행하지 않는다.

## 1. 설치

요구 사항: Python 3.12, [uv](https://docs.astral.sh/uv/).

```sh
uv sync                 # 의존성 설치(.venv 생성)
uv run pytest -q        # 단위·통합 테스트(네트워크 없음)
```

discord.py는 2.5 이상이어야 한다. 파트별 nonce 자동 부착에 필요하며, 기동 시 버전을 확인한다.

## 2. 환경변수

`.env.example`을 `.env`로 복사해 값을 채운다. 봇은 `.env`를 직접 읽지 않는다. 셸에서 내보낸 뒤 실행한다.

```sh
cp .env.example .env
set -a; . ./.env; set +a
```

`.env`는 커밋하지 않는다.

### 필수

| 변수 | 설명 |
|---|---|
| `DISCORD_TOKEN` | 봇 토큰 |
| `DISCORD_GUILD_ID` | 슬래시 커맨드를 sync할 길드 ID |
| `IPO_CHANNEL_ID` | 주간 상장 예정 다이제스트 채널 |
| `RESULT_CHANNEL_ID` | 예측 확정·판정 결과·지연 안내 채널 |
| `ANTHROPIC_API_KEY` | 공시 요약·예측 파싱용 |
| `DART_API_KEY` | OpenDART 키 |
| `KRX_AUTH_KEY` | KRX 정보데이터시스템 Open API 키 |

### 선택(기본값)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `ANTHROPIC_SUMMARY_MODEL` | `claude-haiku-4-5` | 공시 요약 모델 |
| `ANTHROPIC_PARSE_MODEL` | `claude-sonnet-4-5` | 예측 파싱 모델 |
| `KRX_BASE_URL` | `https://data-dbg.krx.co.kr/svc/apis` | KRX API 기본 주소 |
| `DB_PATH` | `./data/ipobot.sqlite3` | SQLite 파일 |
| `WEEKLY_POST_WEEKDAY` | `FRI` | 주간 게시 요일 |
| `WEEKLY_POST_TIME` | `18:00` | 주간 게시 시각(KST) |
| `GRACE_HOURS` | `6` | 주간 게시 재시도 허용 시간 |
| `WEEKLY_RETRY_BACKOFF_MINUTES` | `15` | 주간 게시 실패 후 재시도 간격 |
| `IPO_RANGE_DAYS` | `14` | 다이제스트 대상 기간(일) |
| `BASE_LOOKBACK_TRADING_DAYS` | `3` | 예측 기준일 탐색 범위(거래일) |
| `JUDGE_TIME` | `09:00` | 판정 첫 시도 시각(KST) |
| `KRX_POLL_INTERVAL_MINUTES` | `30` | KRX 미공개 시 재조회 간격 |
| `KRX_POLL_DEADLINE` | `15:00` | 당일 폴링 마감 |
| `JUDGE_MAX_ATTEMPTS` | `5` | 무거래 거래일 상한(N) |
| `JUDGE_STALE_BUSINESS_DAYS` | `10` | KRX 조회 불가 관측 영업일 상한 |
| `SUMMARY_RETRY_DAYS` | `3` | 요약 실패 재시도 일수 |
| `SCHEDULER_TICK_SECONDS` | `60` | 스케줄러 tick 간격 |
| `LOG_LEVEL` | `INFO` | 로그 레벨 |

기동 시 다음을 검증하며, 어긋나면 종료한다.

- `JUDGE_TIME` < `KRX_POLL_DEADLINE`
- `WEEKLY_RETRY_BACKOFF_MINUTES` < `GRACE_HOURS` × 60
- `JUDGE_MAX_ATTEMPTS` ≥ 1, `JUDGE_STALE_BUSINESS_DAYS` ≥ 1

## 3. KRX 서비스 신청 절차

1. KRX 정보데이터시스템 Open API(`data-dbg.krx.co.kr`)에 가입하고 인증키를 발급받는다.
2. 다음 3개 서비스를 각각 이용 신청한다.
   - 유가증권 일별매매정보(`sto/stk_bydd_trd`)
   - 코스닥 일별매매정보(`sto/ksq_bydd_trd`)
   - 코넥스 일별매매정보(`sto/knx_bydd_trd`)
3. **승인에는 시간이 걸린다.** 승인 전 키로 호출하면 401/403이 오고, 봇은 이를 인증 오류(`krx_auth`)로 분류한다. 승인 상태(대기/승인/거부)는 `docs/adr/0001-data-sources.md` §3에 기록한다. 거부되면 대체 출처로 자동 전환하지 않고 운영을 중단한다.
4. 승인 후 `uv run python -m ipobot krx-probe <직전 거래일>`로 3시장이 모두 「공개」로 나오는지 확인하고, 이어서 S0b를 실행한다.

## 4. 명령

모든 명령은 환경변수를 내보낸 셸에서 실행한다.

| 명령 | 필요 환경변수 | 동작 |
|---|---|---|
| `uv run python -m ipobot run` | 필수 전부 | 디스코드 봇 실행 |
| `uv run python -m ipobot krx-probe YYYY-MM-DD [--code 종목코드]` | `KRX_AUTH_KEY`(+`KRX_BASE_URL`) | 3시장 일별매매정보 조회 요약. `--code`를 주면 해당 종목의 관측 분류도 출력. 하나라도 실패하면 종료 코드 1 |
| `uv run python -m ipobot sync-master` | `DART_API_KEY`(+`DB_PATH`) | DART `corpCode.xml`로 `stock_master` 갱신 |
| `uv run python -m ipobot preview-digest [--date YYYY-MM-DD]` | `DART_API_KEY`(+`DB_PATH`, `IPO_RANGE_DAYS`) | 상장 예정 다이제스트를 게시하지 않고 출력. DART 장애면 실패 안내와 종료 코드 1(0건 안내를 내지 않음) |

설정 오류의 종료 코드는 2다.

디스코드 슬래시 커맨드는 두 개다.

- `/upcoming_ipos`: 향후 상장 예정 종목을 즉시 보여준다.
- `/predict`: 예측을 제출한다. 미리보기에서 작성자가 확인해야 저장된다. 미리보기는 10분 뒤 만료되며 재시작해도 「만료됨」이 된다.

## 5. 운영

### 로그

- 형식: `시각 레벨 로거 메시지`. 표준 출력으로 나가며 `LOG_LEVEL`로 조절한다.
- 주요 키워드는 다음과 같다.
  - `OPERATOR_ALERT <kind> <detail>`(ERROR): 운영자 조치가 필요하다. 종류별로 KST 날짜당 1회만 남는다.
  - `krx_closed_day_detected <날짜> evidence=<이후 공개일>`(WARN): KRX 빈 응답으로 미예측 휴장을 기록했다.
  - 주간 게시가 grace 밖으로 밀려 일부 파트만 게시된 채 끝나면 ERROR 로그가 남는다.

### 휴장 override 추가

`holidays` 규칙에 없는 임시 휴장이나, 규칙상 휴일이지만 실제 개장한 날은 DB에 직접 넣는다.

```sh
sqlite3 ./data/ipobot.sqlite3
```

```sql
-- 임시 휴장
INSERT OR REPLACE INTO market_calendar_overrides(day, is_open, note) VALUES ('YYYY-MM-DD', 0, '임시 휴장');
-- 규칙상 휴일이지만 개장
INSERT OR REPLACE INTO market_calendar_overrides(day, is_open, note) VALUES ('YYYY-MM-DD', 1, '개장');
```

override는 규칙 휴일과 `krx_closed_days`보다 우선한다. 판정 거래일은 매 시도마다 재도출하므로 pending 판정에는 다음 시도부터 반영된다. 재시작은 필요 없다.

### `OPERATOR_ALERT` 대응

| kind | 의미 | 조치 |
|---|---|---|
| `krx_auth` | KRX 401/403 | KRX 키와 3개 서비스의 승인 상태를 확인하고, 키를 교체한 뒤 재시작한다. 판정은 pending으로 유지되며 복구 후 catch-up으로 공지된다 |
| `discord_forbidden` | 채널 전송 권한 없음 | 봇의 채널 권한(메시지 보내기·임베드 링크)과 채널 ID를 확인한다. 재시도는 계속된다 |
| `discord_channel_not_found` | Unknown Channel(10003) | `IPO_CHANNEL_ID`/`RESULT_CHANNEL_ID`가 맞는지, 채널이 삭제되지 않았는지 확인한다 |
| `discord_not_found` | 그 밖의 404 | 로그의 `code`/`status`를 확인하고, 채널과 프록시 설정을 점검한다. 재시도는 계속된다 |

**결과 채널에 「⏳ {종목명} 예측 결과: 데이터 출처 인증 오류로 결과 발표가 지연되고 있습니다. 복구되면 결과를 공지합니다.」가 보이면 `KRX_AUTH_KEY`를 확인한다.**

- 지연 안내는 판정마다 1회만 게시된다.
- 인증 오류는 조회 불가 계수(`JUDGE_STALE_BUSINESS_DAYS`)에 넣지 않으므로 「판정 불가」로 굳지 않는다.
- 스펙과의 의도적 차이이며 인증 오류 범주에 한한다(사용자 결정 2026-11-12).

### 휴장 오탐 정정 절차

WARN `krx_closed_day_detected`가 가리킨 날이 실제로는 개장일이었다면 다음을 실행한다.

```sql
INSERT OR REPLACE INTO market_calendar_overrides(day, is_open, note) VALUES ('YYYY-MM-DD', 1, '오탐 정정');
```

- override open이 `krx_closed_days`보다 우선하므로, pending 판정은 다음 시도에 재도출된다.
- **이미 done인 판정은 불변이며 재판정하지 않는다.**
- 기록 정리를 원하면 다음도 함께 실행한다.

```sql
DELETE FROM krx_closed_days WHERE trade_date = 'YYYY-MM-DD';
```

### 판정 규칙 고지

- 판정 거래일은 목표일 이상이면서 KRX 공개·거래가 존재하는 첫 날이다. 목표일이 휴장·무거래면 공지에 「목표일 … 휴장·무거래로 다음 거래일 적용」이 표기된다.
- 결과는 판정 거래일 다음 영업일 오전, KRX 공개 후 공지된다. 금요일 판정분은 월요일에 공지된다.
- **비수정 종가 기준이다.** 무상증자·액면분할 같은 기업행위가 있으면 주가 오차가 왜곡될 수 있다. 시가총액은 영향을 받지 않는다. 수정주가는 지원하지 않는다.

### 판정 불가 사유

| 사유 코드 | 공지 문구 | 조건 |
|---|---|---|
| `halted_suspected` | 거래정지 또는 무거래 추정 | 행은 있지만 거래가 없는(거래량 0·종가 0) 무거래 거래일이 `JUDGE_MAX_ATTEMPTS`(N)일 이어짐 |
| `delisted_suspected` | 상장폐지 추정 | 공개된 시세에 종목 행이 없는 거래일이 N일 이어짐 |
| `krx_unavailable` | KRX 시세를 장기간 조회할 수 없음(시작일·영업일 수 표기) | 봇이 직접 관측한 서로 다른 조회 불가 영업일이 `JUDGE_STALE_BUSINESS_DAYS`(기본 10)에 도달. 봇이 꺼져 있던 기간과 인증 오류는 세지 않음 |
| `invalid_actual` | 실제 시세가 0 이하로 조회되어 오차를 계산할 수 없음 | 실제 값이 0 이하 |

### 수용한 잔여 리스크

- 파트 전송 성공 후 진행 기록 전에 크래시가 나고 nonce 중복 제거 창 밖에서 재시작되면, 그 파트 1개가 중복된다.
- 모호한 전송 실패(타임아웃·5xx)가 파트당 90초 재시도 예산 안에서 해소되지 않았는데 실제로는 게시된 경우, 다음 주기 재시도에서 그 파트가 중복될 수 있다.
- nonce 원본이 삭제된 뒤의 재시도(`NotFound` code 10008)만 전달로 간주해 재게시하지 않는다. 그 외 404는 `OPERATOR_ALERT discord_not_found`로 알리고 재시도한다.
- grace 밖으로 밀린 주간 게시는 일부 파트만 게시된 채 끝날 수 있다(ERROR 로그).
- KRX 데이터 이상(시가총액 `-`·필드 결측)은 해당 시장-일자를 저장하지 않고 재조회한다. 이상이 오래 이어지면 `krx_unavailable`로 판정 불가가 될 수 있다.
- KRX가 일별매매정보를 일자 순서로 공개한다고 가정한다. 이 가정이 틀리면 미공개 일자를 휴장으로 오판할 수 있다. 3시장이 모두 빈 응답이고 이후 날짜가 공개돼야만 휴장으로 기록하며, S0b에서 검증한다. 오판 시 위 정정 절차를 따른다.

## 6. S0b: 실제 KRX fixture 교체 절차

**아직 실행되지 않았다.** 현재 `tests/fixtures/krx_*.json`과 `dart_*.json`은 문서 기반 합성본이다.

- 잠정 형식은 `docs/adr/0001-data-sources.md` §4에 있다.
- KRX 키 승인 직후 운영자가 다음을 수행한다.

1. **미승인·인증 오류 형식 기록**: 잘못된 키로 호출해 HTTP 코드(401/403)와 본문을 기록하고, `auth`로 분류되는지 확인한다.
   ```sh
   KRX_AUTH_KEY=invalid uv run python -m ipobot krx-probe <직전 거래일>   # 「인증 오류」가 출력되어야 함
   curl -s -i -H "AUTH_KEY: $KRX_AUTH_KEY" "$KRX_BASE_URL/sto/stk_bydd_trd?basDd=YYYYMMDD"
   curl -s -i -H "AUTH_KEY: $KRX_AUTH_KEY" "$KRX_BASE_URL/sto/ksq_bydd_trd?basDd=YYYYMMDD"
   curl -s -i -H "AUTH_KEY: $KRX_AUTH_KEY" "$KRX_BASE_URL/sto/knx_bydd_trd?basDd=YYYYMMDD"
   ```
2. **3시장 실제 응답 기록**: 다음 항목을 기록한다.
   - 코드 형식: `ISU_CD`가 6자리인지, `0158S0` 같은 영숫자 코드가 있는지.
   - 숫자 포맷, 결측·`-` 표현의 실제 사례. 실제 사례가 없으면 `krx_anomaly.json`은 합성본을 유지하고 ADR에 명시한다.
   - 현재 거래정지 종목 1개 이상의 행(`krx_halted.json`). 행이 없으면 「부재」 형태를 기록한다.
   - KONEX 무거래 행.
   - 휴장일 빈 응답 형식(직전 공휴일 `basDd`).
3. **fixture 교체**: 기록한 응답에서 키와 개인정보를 제거해 `tests/fixtures/krx_*.json`을 교체하고 `"_provisional": true`를 지운다. DART 키가 있으면 `dart_*.json`도 같은 방식으로 교체한다(ADR PENDING-1).
4. **공개 시각 관측**: 07:00~10:00에 30분 간격으로 직전 거래일을 3시장 모두 조회해 첫 공개 시각을 기록한다.
   ```sh
   uv run python -m ipobot krx-probe <직전 거래일>
   ```
   - 3시장이 동시에 공개되는지 확인한다.
   - 「일자 순서 공개」 가정을 확인한다. 직전 10영업일과 알려진 휴장일 1개를 조회해, 전날이 미공개인데 당일이 공개되는 사례가 없는지 본다.
   - 결과로 `JUDGE_TIME`·`KRX_POLL_DEADLINE` 기본값을 확정한다.
5. **확인**
   ```sh
   uv run pytest -q
   uv run python -m ipobot krx-probe <직전 거래일> --code 0158S0
   ```
   - 실제 형식이 잠정값과 다르면 KRX 어댑터·시세 서비스(필요 시 판정 술어)를 고치는 후속 커밋을 만들고 재검증한다.
6. **중단 조건**
   - 영숫자 코드가 제공되지 않거나 정지 종목 표현을 판별할 수 없으면 중단하고 보고한다.
   - 「일자 순서 공개」 가정이 반증되면, 미예측 휴장 판정을 「override 전용」으로 바꾸는 안을 보고한다.
7. 결과를 `docs/adr/0001-data-sources.md`(§3 승인 상태, §4 확정값, §7 PENDING)에 기록한다.

## 7. 수동 디스코드 스모크 체크리스트

테스트 서버에서 **S0b 완료 후** 수행한다.

1. 기동 로그에 길드 sync 성공과 설정 요약(비밀값 마스킹)이 찍힌다.
2. `WEEKLY_POST_TIME`을 현재+2분으로 두면 1회 게시된다. 재시작해도 재게시하지 않는다.
3. `/upcoming_ipos` 결과가 2와 같은 형식이다.
4. `/predict`에 예시를 넣으면 미리보기에 4시점, 기준일(= KRX 최신 공개일), 기준값, 공지 시점 안내가 나온다.
5. 제출 직후 작성자에게만 보이는 「미리보기를 채널에 게시했습니다」 안내가 뜨고, 미리보기는 채널에 공개로 게시된다. 거부·해석 오류는 작성자에게만 보인다. 다른 계정이 확인을 누르면 거부된다.
6. 작성자가 확인하면 공개 확정 메시지가 나온다. 같은 텍스트를 재제출하면 중복으로 거부된다.
7. 거부 사례를 확인한다.
   - 6개월 항목을 빼면 시점 누락으로 거부된다.
   - 없는 종목명이면 미인식으로 거부된다.
   - `시총 3,500억` 텍스트에 단위를 바꾼 값을 넣으면 금액 불일치로 거부된다.
8. 취소하거나 10분 방치하면 저장되지 않는다. 미리보기를 띄운 채 봇을 재시작하고 확인을 누르면 「만료됨」이 나온다.
9. 테스트 judgment의 시도 시각과 목표일을 과거로 바꾸면 다음 tick에 결과가 공지된다.
   ```sql
   UPDATE judgments SET next_attempt_at = '2000-01-01T00:00:00+00:00', target_date = 'YYYY-MM-DD' WHERE id = <id>;
   ```
10. `KRX_AUTH_KEY`를 잘못 넣고 9를 반복한다.
    - `last_error='krx_auth'`와 `OPERATOR_ALERT krx_auth` 로그가 남는다.
    - 채널에 지연 안내가 **1회만** 게시되고 판정은 pending으로 남는다.
    - `JUDGE_STALE_BUSINESS_DAYS=1`로 두고 마감을 현재 직전으로 두어도 「판정 불가」가 게시되지 않는다.
    - 키를 고친 뒤 재시작하면 결과가 공지된다.
11. `DART_API_KEY`를 잘못 넣고 2를 반복하면 게시 0건, `job_runs` failed가 남는다. 키를 복구하면 백오프 후 1회 게시된다.
12. KRX 장기 조회 불가 공지를 확인한다(개장일에 실행).
    - 키를 복구하고 `KRX_BASE_URL=http://127.0.0.1:9`로 바꾼다.
    - `JUDGE_STALE_BUSINESS_DAYS=1`, `KRX_POLL_DEADLINE`을 현재 직전으로 둔다.
    - 9를 반복하면 「판정 불가(KRX 시세를 장기간 조회할 수 없음)」가 공지된다.

## 8. 개발

- 테스트: `uv run pytest -q`. `tests/integration/test_end_to_end.py`가 실제 sqlite·서비스 배선과 fake KRX/DART/LLM/디스코드 채널로 재시작·크래시·장애 시나리오를 검증한다.
- 설계 결정: `docs/adr/`.
- 스키마: `src/ipobot/db/migrations/0001_init.sql`.
