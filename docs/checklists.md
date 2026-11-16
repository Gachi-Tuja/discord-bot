# 검증 체크리스트

한 번 수행하고 결과를 기록하는 절차다.

## 1. S0b: 실제 KRX fixture 교체 절차

**아직 실행되지 않았다.** 현재 `tests/fixtures/krx_*.json`과 `dart_*.json`은 문서 기반 합성본이다.

- 잠정 형식은 [ADR §4](adr/0001-data-sources.md)에 있다.
- KRX 키는 승인됐다(2026-11-16 확인). 운영자가 다음을 수행한다.

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

## 2. 수동 디스코드 스모크 체크리스트

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
