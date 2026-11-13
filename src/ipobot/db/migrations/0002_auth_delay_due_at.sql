-- 0002_auth_delay_due_at: 인증 오류 지연 안내 필요 상태를 last_error와 분리해 저장한다(Part A1).
-- last_error는 이후 시도(판정 예외 'judging' 등)가 덮어쓰므로, 재시작 복원은 이 컬럼을 근거로 한다.
ALTER TABLE judgments ADD COLUMN auth_delay_due_at TEXT;  -- 안내가 필요해진 시각(ISO-8601 UTC). 안내 완료·조건 해소 시 NULL

-- 기존 DB: 마지막 시도가 인증 오류였던 미안내 pending 행은 안내 필요 상태로 옮긴다.
UPDATE judgments
   SET auth_delay_due_at = updated_at
 WHERE status = 'pending' AND auth_delay_notified_at IS NULL AND last_error = 'krx_auth';
