# 운영 매뉴얼

서버 운영, 배포, 장애 대응, 판정 규칙을 다룬다. 설치와 명령은 [README](../README.md)를 본다.

## 운영 서버

| 항목 | 값 |
|---|---|
| 호스트 | `susc-crow`(`ssh crow`), 계정 `ubuntu` |
| 코드 | `~/ipobot`. 최신 릴리스 태그가 체크아웃돼 있다 |
| 환경변수 | `~/ipobot/.env`. systemd가 `EnvironmentFile`로 읽는다 |
| DB | `.env`의 `DB_PATH` |
| 봇 | `ipobot.service`(`deploy/ipobot.service`) |
| 자동 배포 | `ipobot-update.timer` → `ipobot-update.service`(`deploy/`) |

```sh
systemctl status ipobot                  # 상태
journalctl -u ipobot -f                  # 봇 로그
journalctl -u ipobot-update -n 50        # 배포 로그
sudo systemctl restart ipobot            # 재시작
```

서버에서 CLI([README](../README.md#명령))를 돌릴 때는 환경변수를 내보내고 `.venv`의 파이썬을 쓴다.

```sh
cd ~/ipobot && set -a; . ./.env; set +a
.venv/bin/python -m ipobot preview-digest
```

서버에는 `sqlite3` CLI가 없다. 이 문서의 SQL은 파이썬으로 실행한다. 쓰기 전에는 봇을 멈춘다.

```sh
sudo systemctl stop ipobot
.venv/bin/python - <<'EOF'
import os, sqlite3
conn = sqlite3.connect(os.environ["DB_PATH"])
print(conn.execute("SELECT ...").fetchall())   # 여기에 SQL
conn.commit()
EOF
sudo systemctl start ipobot
```

## 릴리스와 자동 배포

서버는 GitHub의 **최신 릴리스**를 따라간다. `main`에 푸시만 해서는 배포되지 않는다.

릴리스 절차:

1. `pyproject.toml`의 `version`을 올리고 `uv lock`을 실행해 커밋한다.
2. `main`을 푸시한 뒤 같은 버전으로 릴리스를 만든다: `gh release create v0.2.0 --target main --generate-notes`.
3. 5분 안에 서버가 배포한다. 확인: `journalctl -u ipobot-update -n 50`.

서버의 `ipobot-update.timer`가 5분마다 `deploy/update.sh`를 실행한다. 스크립트는 다음 순서로 동작한다.

1. 최신 릴리스 태그가 지금 체크아웃된 태그와 같으면 아무것도 하지 않는다.
2. 다르면 태그를 체크아웃하고 `uv sync --frozen` 후 테스트를 돌린다.
3. 봇을 재시작하고 10초 뒤 active인지 확인한다.

테스트가 실패하거나 재시작 후 active가 아니면 이전 커밋으로 되돌리고 다시 재시작한다. 그 릴리스는 5분마다 다시 시도되므로, 고친 릴리스를 새로 내야 멈춘다.

서버에서 추적 파일을 직접 고치면 자동 배포가 멈추고 로그에 수정된 파일이 남는다. `.env`와 `data/`는 추적하지 않으므로 영향이 없다.

설치(최초 1회, 서버에서):

```sh
git clone https://github.com/Gachi-Tuja/discord-bot.git ~/ipobot && cd ~/ipobot
git checkout "$(git describe --tags --abbrev=0)"
uv sync --frozen && cp .env.example .env    # .env 채우기
sudo cp deploy/ipobot.service deploy/ipobot-update.service deploy/ipobot-update.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ipobot.service ipobot-update.timer
```

스크립트는 `ubuntu` 계정으로 돌며 재시작에 암호 없는 `sudo`가 필요하다. 저장소가 공개라 GitHub 인증은 필요 없다.

## 로그

- 형식: `시각 레벨 로거 메시지`. 표준 출력으로 나가며 `LOG_LEVEL`로 조절한다.
- 주요 키워드는 다음과 같다.
  - `OPERATOR_ALERT <kind> <detail>`(ERROR): 운영자 조치가 필요하다. 종류별로 KST 날짜당 1회만 남는다.
  - `krx_closed_day_detected <날짜> evidence=<이후 공개일>`(WARN): KRX 빈 응답으로 미예측 휴장을 기록했다.
  - 주간 게시가 grace 밖으로 밀려 일부 파트만 게시된 채 끝나면 ERROR 로그가 남는다.

## 휴장 override 추가

`holidays` 규칙에 없는 임시 휴장이나, 규칙상 휴일이지만 실제 개장한 날은 DB에 직접 넣는다(서버에서는 「운영 서버」의 파이썬 방식으로 실행).

```sql
-- 임시 휴장
INSERT OR REPLACE INTO market_calendar_overrides(day, is_open, note) VALUES ('YYYY-MM-DD', 0, '임시 휴장');
-- 규칙상 휴일이지만 개장
INSERT OR REPLACE INTO market_calendar_overrides(day, is_open, note) VALUES ('YYYY-MM-DD', 1, '개장');
```

override는 규칙 휴일과 `krx_closed_days`보다 우선한다. 판정 거래일은 매 시도마다 재도출하므로 pending 판정에는 다음 시도부터 반영된다. 재시작은 필요 없다.

## `OPERATOR_ALERT` 대응

| kind | 의미 | 조치 |
|---|---|---|
| `krx_auth` | KRX 401/403 | KRX 키와 3개 서비스의 승인 상태를 확인하고, 키를 교체한 뒤 재시작한다. 판정은 pending으로 유지되며 복구 후 catch-up으로 공지된다 |
| `discord_forbidden` | 채널 전송 권한 없음 | 봇의 채널 권한(메시지 보내기·임베드 링크·메시지 기록 보기)과 채널 ID를 확인한다. 재시도는 계속된다 |
| `discord_channel_not_found` | Unknown Channel(10003) | `IPO_CHANNEL_ID`/`RESULT_CHANNEL_ID`가 맞는지, 채널이 삭제되지 않았는지 확인한다 |
| `discord_not_found` | 그 밖의 404 | 로그의 `code`/`status`를 확인하고, 채널과 프록시 설정을 점검한다. 재시도는 계속된다 |
| `discord_history_forbidden` | 채널 기록 조회 권한 없음 | 봇에 메시지 기록 보기 권한을 준다. 없는 동안 재전송 중복 확인을 건너뛴다 |
| `progress_record_failing` | 같은 게시의 진행 기록(DB 쓰기)이 연속 실패 | DB 파일 권한·디스크 공간·잠금을 점검한다. 복구 전까지 기록 확인 없이는 재전송하지 않는다 |

**결과 채널에 「⏳ {종목명} 예측 결과: 데이터 출처 인증 오류로 결과 발표가 지연되고 있습니다. 복구되면 결과를 공지합니다.」가 보이면 `KRX_AUTH_KEY`를 확인한다.**

- 지연 안내는 판정마다 1회만 게시된다.
- 인증 오류는 조회 불가 계수(`JUDGE_STALE_BUSINESS_DAYS`)에 넣지 않으므로 「판정 불가」로 굳지 않는다.
- 스펙과의 의도적 차이이며 인증 오류 범주에 한한다(사용자 결정 2026-11-12).

## 휴장 오탐 정정 절차

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

## 판정 규칙 고지

- 판정 거래일은 목표일 이상이면서 KRX 공개·거래가 존재하는 첫 날이다. 목표일이 휴장·무거래면 공지에 「목표일 … 휴장·무거래로 다음 거래일 적용」이 표기된다.
- 결과는 판정 거래일 다음 영업일 오전, KRX 공개 후 공지된다. 금요일 판정분은 월요일에 공지된다.
- **비수정 종가 기준이다.** 무상증자·액면분할 같은 기업행위가 있으면 주가 오차가 왜곡될 수 있다. 시가총액은 영향을 받지 않는다. 수정주가는 지원하지 않는다.

## 판정 불가 사유

| 사유 코드 | 공지 문구 | 조건 |
|---|---|---|
| `halted_suspected` | 거래정지 또는 무거래 추정 | 행은 있지만 거래가 없는(거래량 0·종가 0) 무거래 거래일이 `JUDGE_MAX_ATTEMPTS`(N)일 이어짐 |
| `delisted_suspected` | 상장폐지 추정 | 공개된 시세에 종목 행이 없는 거래일이 N일 이어짐 |
| `krx_unavailable` | KRX 시세를 장기간 조회할 수 없음(시작일·영업일 수 표기) | 봇이 직접 관측한 서로 다른 조회 불가 영업일이 `JUDGE_STALE_BUSINESS_DAYS`(기본 10)에 도달. 봇이 꺼져 있던 기간과 인증 오류는 세지 않음 |
| `invalid_actual` | 실제 시세가 0 이하로 조회되어 오차를 계산할 수 없음 | 실제 값이 0 이하 |

## 수용한 잔여 리스크

- 재전송 중복은 채널 기록 확인으로 막는다. 재시도·재시작 시 이어 보낼 파트를 전송하기 전에 채널 최근 메시지(최대 100개, 주간은 슬롯 1분 전, 공지·안내는 목표일 0시 KST 이후)에서 봇이 쓴 같은 내용의 메시지를 찾고, 있으면 다시 보내지 않고 그 메시지로 기록한다. 따라서 nonce 중복 제거 창(약 5분) 밖의 크래시 재시작, 진행 기록(DB 쓰기) 연속 실패, 90초 예산 밖의 모호한 실패(타임아웃·5xx)도 중복 게시로 이어지지 않는다. 남는 한계:
  - 봇에 **메시지 기록 보기(Read Message History)** 권한이 없거나 기록 조회가 실패하면 `OPERATOR_ALERT discord_history_forbidden`(권한) 또는 경고 로그를 남기고 예전처럼 전송한다(nonce 창 밖이면 중복될 수 있다).
  - 같은 대상의 진행 기록이 2회 연속 실패하면 `OPERATOR_ALERT progress_record_failing`을 1회 내고, 성공할 때까지는 기록 조회에 실패하면 전송하지 않는다. 이 보류 상태는 메모리에만 있어 재시작하면 풀린다.
  - 그 사이 채널에 메시지가 100개 넘게 쌓이거나 누군가 원본을 지우면 찾지 못해 다시 게시한다.
- nonce 원본이 삭제된 뒤의 재시도(`NotFound` code 10008)만 전달로 간주해 재게시하지 않는다. 그 외 404는 `OPERATOR_ALERT discord_not_found`로 알리고 재시도한다.
- grace 밖으로 밀린 주간 게시는 일부 파트만 게시된 채 끝날 수 있다(ERROR 로그).
- KRX 데이터 이상(시가총액 `-`·필드 결측)은 해당 시장-일자를 저장하지 않고 재조회한다. 이상이 오래 이어지면 `krx_unavailable`로 판정 불가가 될 수 있다.
- KRX가 일별매매정보를 일자 순서로 공개한다고 가정한다. 이 가정이 틀리면 미공개 일자를 휴장으로 오판할 수 있다. 3시장이 모두 빈 응답이고 이후 날짜가 공개돼야만 휴장으로 기록하며, S0b([체크리스트](checklists.md))에서 검증한다. 오판 시 「휴장 오탐 정정 절차」를 따른다.
