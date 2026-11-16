# ipobot — 공모주 상장 알림·주가 예측 디스코드 봇

이 봇은 세 가지 일을 한다.

- **주간 다이제스트**: 매주 정해진 시각(기본 금요일 18시 KST)에 향후 1주간의 상장 예정 종목과 회사 한 줄 요약을 게시한다.
  - 상장일은 DART 증권발행실적보고서에서 얻는다. 보고서가 상장 약 1주 전에 접수되므로 다음 주 후반 상장 종목은 빠질 수 있다.
  - 요약은 투자설명서·증권신고서의 `II. 사업의 내용`을 LLM으로 요약한 것이다.
- **예측 제출**: `/predict`로 상장 종목의 1·3·6·12개월 뒤 주가·시가총액 예측을 받는다.
- **판정**: 목표일이 지나면 KRX 일별매매정보 종가(비수정)로 판정해 결과를 공지한다.

현재 `susc-crow`에서 운영 중이며 GitHub 최신 릴리스가 자동 배포된다. 테스트 fixture는 아직 합성본이다([검증 체크리스트](docs/checklists.md)의 S0b).

| 문서 | 내용 |
|---|---|
| [docs/operations.md](docs/operations.md) | 운영 서버, 릴리스·자동 배포, 장애 대응, 판정 규칙 |
| [docs/checklists.md](docs/checklists.md) | KRX fixture 교체(S0b), 수동 디스코드 스모크 |
| [docs/adr/](docs/adr/) | 설계 결정 |

## 설치

요구 사항: Python 3.12, [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run pytest -q        # 네트워크 없이 돈다
cp .env.example .env    # 값 채우기
set -a; . ./.env; set +a
uv run python -m ipobot run
```

봇은 `.env`를 직접 읽지 않으므로 셸에서 내보낸 뒤 실행한다. `.env`는 커밋하지 않는다.

## 환경변수

필수 값은 아래와 같다. 선택 값과 기본값은 [.env.example](.env.example)에 설명과 함께 있다. 기동 시 값의 범위와 상호 관계를 검증하며, 어긋나면 종료 코드 2로 끝난다.

| 변수 | 설명 |
|---|---|
| `DISCORD_TOKEN` | 봇 토큰 |
| `DISCORD_GUILD_ID` | 슬래시 커맨드를 sync할 길드 ID |
| `IPO_CHANNEL_ID` | 주간 다이제스트 채널 |
| `RESULT_CHANNEL_ID` | 예측 확정·판정 결과·지연 안내 채널 |
| `ANTHROPIC_API_KEY` | 공시 요약·예측 파싱 |
| `DART_API_KEY` | [OpenDART](https://opendart.fss.or.kr) 키 |
| `KRX_AUTH_KEY` | [KRX Open API](https://openapi.krx.co.kr) 키 |

KRX 키는 [주식 서비스 목록](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES002_S1.cmd)에서 유가증권·코스닥·코넥스 **일별매매정보** 3개 서비스를 각각 이용 신청해 승인받아야 쓸 수 있다. 신청 현황은 [마이페이지](https://openapi.krx.co.kr/contents/OPP/MYPG/mypage/OPPMYPG001.cmd)에서 본다. 승인 전에는 401/403이 오고 봇은 이를 `krx_auth`로 분류한다. 승인 여부는 `krx-probe`로 확인한다.

## 명령


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


## 개발

- `tests/integration/test_end_to_end.py`가 실제 sqlite·서비스 배선과 fake KRX/DART/LLM/디스코드로 재시작·크래시·장애 시나리오를 검증한다.
- 스키마는 `src/ipobot/db/migrations/NNNN_*.sql`이다. 시작 시 `PRAGMA user_version`보다 큰 번호를 순서대로 적용한다. 새 변경은 다음 번호 파일로 추가하고 기존 파일은 고치지 않는다.
- `main` 푸시만으로는 배포되지 않는다. 릴리스 절차는 [운영 매뉴얼](docs/operations.md#릴리스와-자동-배포)에 있다.
