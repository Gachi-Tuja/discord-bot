# ADR 0001: 데이터 소스와 판정 정책 근거

- 상태: **Provisional** (S0a 초안, 2026-11-12). 실제 키 확인 항목은 아래 「PENDING」 절에 남긴다.
- 범위: DART(상장 예정 일정), KRX OpenAPI(시세·판정), 판정 정책 가정, stale 가드.

## 1. 환경 상태 (S0a 실행 시점)

| 키 | 상태 |
|---|---|
| `DART_API_KEY` | 없음. 실제 응답을 받지 못했다 |
| `KRX_AUTH_KEY` | 없음. KRX OpenAPI 서비스 이용 신청 **미실시**(키 없음) |
| `DISCORD_TOKEN`, `ANTHROPIC_API_KEY` | 없음 |

따라서 이 단계의 fixture는 모두 **문서 기반 합성(synthetic) fixture**이며 파일마다 `"_provisional": true`를 둔다.
테스트는 실제 외부 API를 호출하지 않는다.

## 2. DART 결정

- 수집 경로
  1. `list.json`(`pblntf_detail_ty=C001`, 증권신고서(지분증권))로 최근 신고서 목록을 받는다. `rcept_no`를 키로 쓴다.
     정정 신고서(`report_nm`이 `[기재정정]`으로 시작, `rm`에 `정`)는 같은 회사의 최신 `rcept_no`가 이긴다.
  2. `estkRs.json`(지분증권 주요정보)으로 청약기일(`sbd`)·납입기일(`pymd`)을 얻는다.
     **OpenDART 개발가이드상 `estkRs.json`에는 상장예정일 필드가 없다.**
  3. 상장예정일은 `document.xml`(원문 zip) 텍스트에서 `상장예정일` 라벨 뒤의 `YYYY년 MM월 DD일`을 추출한다
     (fixture: `tests/fixtures/dart_document_listing.json`; 개정 후 기대값 `2026-11-18`). 추출 실패 시 해당 건은 다이제스트에서 제외하고 WARN 로그를 남긴다.
  4. `corpCode.xml`로 `stock_master`를 채운다. 영숫자 종목코드(예: `0158S0`)가 들어올 수 있다고 가정한다
     (fixture: `dart_corpcode_sample.json`). 신규 상장은 늦게 반영될 수 있어 R7 3단계(KRX `ISU_NM` 매칭)로 보완한다.
- 잠정 결정: 「list.json → document.xml 원문 추출」을 상장예정일 획득 경로로 채택한다. estkRs.json은 보조 정보용이다.
- **개정 (2026-11-13, 실키 검증 결과)**: 실제 증권신고서·투자설명서 원문에는 `상장예정일`이 없었다(PENDING-1 실패).
  상장일은 상장 약 1주 전에 접수되는 **증권발행실적보고서**에만 `3. 상장일(매매개시일) YYYY년 MM월 DD일 (예정)` 형태로 확정 기재된다.
  사용자 승인으로 경로를 바꾼다.
  1. list.json(C001) 중 `report_nm`에 `증권발행실적보고서`가 든 건만 상장일 출처로 쓴다(회사별 최신 접수번호).
  2. 원문에서 `상장일(매매개시일)` 라벨 뒤 날짜를 추출한다. 유상증자 실적보고서는 `주권상장 및 유통 예정일`·`신주상장 예정일`을 써서 자연히 제외된다.
  3. 요약 원문은 같은 회사의 최신 증권신고서·투자설명서(철회·첨부정정 제외)를 `ipo_listings.summary_rcept_no`(마이그레이션 0003)로 연결한다. 없으면 실적보고서를 쓴다.
  4. 실적보고서가 상장 약 1주 전에 나오므로 `IPO_RANGE_DAYS` 기본값을 14에서 7로 줄인다. 금요일 게시 시점에 실적보고서가 아직 없는 다음 주 후반 상장 종목은 빠질 수 있다.
- 게이트 1: 경로는 문서상 확정했으나 **실제 응답으로 검증하지 못했다**(PENDING-1). 실키 검증에서 원문에 `상장예정일`이 일관되게 없으면 게이트 1(사용자 승인)로 올린다.

## 3. KRX 신청 상태

- 상태: **승인**. 2026-11-16에 운영 키로 `krx-probe 2026-11-13`을 실행해 3시장 모두 「공개」를 확인했다(KOSPI 936행, KOSDAQ 1834행, KONEX 107행, 이상 행 0). 영숫자 코드 `0162D0`도 조회됐다. S0b(실제 fixture 기록)는 아직 남아 있다.
- 이하는 승인 전 운영 계획이다.
- 계획상 「승인 대기」와 같이 취급한다. 중단하지 않고 S1부터 fake/합성 fixture로 진행하며 CP-KRX(S0b)를 예약한다.
- 승인 **거부** 시에는 즉시 중단하고 보고한다(대체 출처 자동 전환 금지).
- 키 확보 후 실행할 명령(미승인 응답 형식, 401/403 구분 기록용):

```sh
curl -s -i -H "AUTH_KEY: $KRX_AUTH_KEY" "$KRX_BASE_URL/sto/stk_bydd_trd?basDd=YYYYMMDD"
curl -s -i -H "AUTH_KEY: $KRX_AUTH_KEY" "$KRX_BASE_URL/sto/ksq_bydd_trd?basDd=YYYYMMDD"
curl -s -i -H "AUTH_KEY: $KRX_AUTH_KEY" "$KRX_BASE_URL/sto/knx_bydd_trd?basDd=YYYYMMDD"
```

## 4. Provisional 값 (승인 전 잠정, CP-KRX에서 확정)

- 종목코드: 6자리 `[0-9A-Z]{6}`. 입력의 `A` 접두사는 제거한다. ISIN(`KR7` + 6자리 + …)은 4~9번째 문자를 쓴다.
- 숫자 필드: 콤마가 있는 문자열 → int. 빈 문자열·`-`는 0. 키 결측·비숫자는 행 이상(R9 `data_anomaly`).
- 필드명: `BAS_DD`, `ISU_CD`, `ISU_NM`, `MKT_NM`, `TDD_CLSPRC`, `TDD_OPNPRC`, `ACC_TRDVOL`, `MKTCAP`, `LIST_SHRS`.
- 빈 응답: `{"OutBlock_1": []}`.
- 정지 종목: 행은 있고 `ACC_TRDVOL="0"`, `TDD_OPNPRC="0"`.
- 공개 시각: 08:00 전후로 가정, `JUDGE_TIME=09:00` 유지.
- 엔드포인트: KOSPI=`sto/stk_bydd_trd`, KOSDAQ=`sto/ksq_bydd_trd`, KONEX=`sto/knx_bydd_trd`.
- 인증 오류: 401과 403을 모두 `FetchError('auth')`로 분류한다(실제 어느 쪽이 오는지는 PENDING-3).

합성 fixture 목록(`tests/fixtures/`):

| 파일 | 내용 |
|---|---|
| `krx_kospi_sample.json` | KOSPI 정상 3행 |
| `krx_kosdaq_alnum.json` | KOSDAQ, 영숫자 코드 `0158S0` 포함 |
| `krx_konex_sample.json` | KONEX 정상 1행 + 무거래 1행 |
| `krx_empty.json` | `{"OutBlock_1": []}` |
| `krx_halted.json` | 정지 행(`ACC_TRDVOL="0"`, `TDD_OPNPRC="0"`) + 정상 행 |
| `krx_anomaly.json` | vol>0·close>0·`MKTCAP="-"` 행, `MKTCAP` 키 결측 행, 정상 행 |
| `dart_list_c001.json` | list.json(C001) 3건(증권신고서·증권발행실적보고서·정정 신고서) |
| `dart_list_empty.json` | status 013(조회 데이터 없음) |
| `dart_estkrs.json` | estkRs.json 일반사항·증권의종류·인수인정보 그룹 |
| `dart_document_listing.json` | 실제 증권발행실적보고서 발췌(`상장일(매매개시일)` 포함) |
| `dart_document_rights_offering.json` | 실제 유상증자 실적보고서 발췌(상장일 추출 안 됨) |
| `dart_corpcode_sample.json` | corpCode 행(영숫자 코드 포함) |

## 5. 판정 정책 근거

- 가정: **KRX는 일별매매정보를 일자 순서로 공개한다.** 따라서 「목표일 이상 캘린더 개장일 중 KRX 스냅샷이 공개·비어있지 않은 첫 날」을 판정 거래일로 채택하고,
  비어있지만 이후 날짜가 공개된 날은 미예측 휴장으로 본다(기록 직전 negative 캐시 우회 재조회로 확정).
- 검증 계획(CP-KRX): 직전 10영업일과 알려진 휴장일 1개를 3시장 모두 조회해 (a) 휴장일이 빈 응답인지, (b) 공개가 일자 순서인지, (c) 시장별 공개 시각을 기록한다.
- 캘린더(`holidays.KR` + 근로자의날 + 연말 휴장 + override + `krx_closed_days`)는 가장 이른 시도 시각만 정하며, 판정일은 매 시도마다 KRX 관측으로 재도출한다.

## 6. stale 가드 선택

- **1안(관측 기반 카운터)을 택했다.** `judgments.unavailable_since/unavailable_last_day/unavailable_days`로
  봇이 실제로 미가용 프런티어를 관측한 서로 다른 영업일(개장일, `KRX_POLL_DEADLINE` 이후 시도)만 센다.
- 기각: 목표일·첫 시도일 기준 경과 계산. 봇 다운타임 후 재시작해 일시 오류 1회만으로 `판정 불가`가 나는 반례가 있다.
- KRX 인증 오류(401/403)는 계수하지 않는다. 판정은 pending으로 남고, 판정 채널에 1회 지연 안내
  (`judgments.auth_delay_notified_at`, `auth_delay_parts_sent`)와 `OPERATOR_ALERT krx_auth` 로그를 남긴다(사용자 결정 C, 2026-11-12).

## 7. PENDING (사람의 조치 필요)

| ID | 항목 | 필요한 조치 |
|---|---|---|
| PENDING-1 | DART 실키 검증 | `DART_API_KEY` 발급 후 list.json(C001)·estkRs.json·document.xml·corpCode.xml 실제 응답 저장, 원문의 `상장예정일` 추출 성공률 확인, 합성 `dart_*.json` 교체 |
| PENDING-2 | KRX 서비스 이용 신청 | 유가·코스닥·코넥스 일별매매정보 3개 서비스 신청, 승인 상태(대기/승인/거부)를 이 ADR에 기록 |
| PENDING-3 | KRX 미승인/인증 오류 형식 | 키 확보 후 위 curl로 HTTP 코드(401/403)와 본문 기록 |
| PENDING-4 | KRX 실제 응답 형식(CP-KRX, S0b) | 필드명·숫자 형식·빈 응답·정지 종목·공개 시각 확인 후 `krx_*.json` 교체, `JUDGE_TIME` 조정 |
