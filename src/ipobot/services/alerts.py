"""운영자 경보: `OPERATOR_ALERT` ERROR 로그를 종류별로 하루(KST 날짜) 1회만 남긴다."""

from __future__ import annotations

import logging
from datetime import date

from ipobot.clock import Clock, SystemClock, today_kst

log = logging.getLogger(__name__)


class OperatorAlerts:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._last_alerted: dict[str, date] = {}

    def alert(self, kind: str, detail: str) -> bool:
        """경보를 남겼으면 True, 같은 날 이미 남긴 종류라 생략했으면 False."""
        today = today_kst(self._clock)
        if self._last_alerted.get(kind) == today:
            log.debug("OPERATOR_ALERT 생략(오늘 이미 기록) kind=%s detail=%s", kind, detail)
            return False
        self._last_alerted[kind] = today
        log.error("OPERATOR_ALERT %s %s", kind, detail)
        return True


_default = OperatorAlerts(SystemClock())


def set_default(alerts: OperatorAlerts) -> None:
    """컴포지션 루트가 주입 시계를 쓰는 인스턴스로 바꾼다."""
    global _default
    _default = alerts


def operator_alert(kind: str, detail: str) -> bool:
    return _default.alert(kind, detail)
