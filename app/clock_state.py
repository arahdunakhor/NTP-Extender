import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .ntp_utils import unix_to_ntp_timestamp


@dataclass
class ServerStatus:
    host: str
    last_checked_at: Optional[float] = None
    ok: bool = False
    offset_sec: Optional[float] = None
    delay_sec: Optional[float] = None
    stratum: Optional[int] = None
    error: Optional[str] = None


@dataclass
class SyncSummary:
    synced: bool = False
    offset_sec: float = 0.0
    dispersion_sec: Optional[float] = None
    root_delay_sec: Optional[float] = None
    root_dispersion_sec: Optional[float] = None
    stratum: int = 16
    last_sync_at: Optional[float] = None
    sync_attempt_ok_count: int = 0
    sync_attempt_fail_count: int = 0


class ClockState:
    """
    Содержит вычисленную "идеальную" временную шкалу для NTP-ответов.
    В контейнере мы обычно не можем менять системное время, поэтому отвечаем клиентам
    скорректированными NTP timestamp'ами на основе вычисленного offset.
    """

    def __init__(self, timezone: str, ntp_stratum_bad: int = 16, ntp_stratum_good: int = 1) -> None:
        self._lock = threading.Lock()

        self.timezone = timezone
        self.ntp_stratum_bad = ntp_stratum_bad
        self.ntp_stratum_good = ntp_stratum_good

        # Последний вычисленный offset относительно системного времени.
        self._offset_sec: float = 0.0

        # База для быстрого расчёта "текущего времени" без syscalls.
        self._base_unix: float = time.time()
        self._base_mono: float = time.monotonic()

        self._summary = SyncSummary()

        # Справочный NTP timestamp (reference timestamp) фиксируется на момент последней синхронизации.
        self._reference_ntp: bytes = b"\x00" * 8

        self._server_status: Dict[str, ServerStatus] = {}

        # Текущие значения для root delay/dispersion в NTP заголовке.
        self._root_delay_ntp_units: int = 0
        self._root_dispersion_ntp_units: int = 0

        self._started_at: float = time.time()

    def uptime_sec(self) -> float:
        return max(0.0, time.time() - self._started_at)

    def set_timezone(self, timezone: str) -> None:
        with self._lock:
            self.timezone = timezone

    def set_server_status(self, servers: List[str]) -> None:
        with self._lock:
            current = self._server_status
            for host in servers:
                if host not in current:
                    current[host] = ServerStatus(host=host)
            # Удаляем статусы для серверов, которых больше нет в конфиге.
            for host in list(current.keys()):
                if host not in servers:
                    del current[host]

    def update_server_check(self, host: str, update: Dict[str, Any]) -> None:
        with self._lock:
            if host not in self._server_status:
                self._server_status[host] = ServerStatus(host=host)
            st = self._server_status[host]
            st.last_checked_at = update.get("last_checked_at", st.last_checked_at)
            st.ok = update.get("ok", st.ok)
            st.offset_sec = update.get("offset_sec", st.offset_sec)
            st.delay_sec = update.get("delay_sec", st.delay_sec)
            st.stratum = update.get("stratum", st.stratum)
            st.error = update.get("error", st.error)

    def apply_successful_sync(
        self,
        *,
        chosen_offset_sec: float,
        dispersion_sec: Optional[float],
        root_delay_sec: Optional[float],
        root_dispersion_sec: Optional[float],
        synced: bool,
        stratum: int,
        last_sync_at: float,
        attempt_ok: bool,
    ) -> None:
        """
        Зафиксировать новую временную шкалу.
        base_unix хранит "идеальное" Unix-время (в секундах), а base_mono — monotonic.
        """
        with self._lock:
            self._offset_sec = chosen_offset_sec
            now_unix = time.time()
            self._base_unix = now_unix + chosen_offset_sec
            self._base_mono = time.monotonic()

            self._summary.synced = synced
            self._summary.offset_sec = chosen_offset_sec
            self._summary.dispersion_sec = dispersion_sec
            self._summary.root_delay_sec = root_delay_sec
            self._summary.root_dispersion_sec = root_dispersion_sec
            self._summary.stratum = stratum
            self._summary.last_sync_at = last_sync_at

            # В режиме "не синхронизировано" reference timestamp обычно стоит обнулять.
            if synced:
                ref_ntp_sec, ref_ntp_frac = unix_to_ntp_timestamp(self._base_unix)
                self._reference_ntp = ref_ntp_sec.to_bytes(4, "big") + ref_ntp_frac.to_bytes(4, "big")
            else:
                self._reference_ntp = b"\x00" * 8

            if attempt_ok:
                self._summary.sync_attempt_ok_count += 1
            else:
                self._summary.sync_attempt_fail_count += 1

            # root delay/dispersion в NTP заголовке — фиксированная точка 16:16 (шаг 1/65536 секунды)
            self._root_delay_ntp_units = int((root_delay_sec or 0.0) * 65536)
            self._root_dispersion_ntp_units = int((root_dispersion_sec or 0.0) * 65536)

    def current_summary(self) -> SyncSummary:
        with self._lock:
            # Копируем поля, чтобы наружный код не зависел от lock'а.
            return SyncSummary(
                synced=self._summary.synced,
                offset_sec=self._summary.offset_sec,
                dispersion_sec=self._summary.dispersion_sec,
                root_delay_sec=self._summary.root_delay_sec,
                root_dispersion_sec=self._summary.root_dispersion_sec,
                stratum=self._summary.stratum,
                last_sync_at=self._summary.last_sync_at,
            )

    def to_api_status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "timezone": self.timezone,
                "synced": self._summary.synced,
                "offset_sec": self._summary.offset_sec,
                "dispersion_sec": self._summary.dispersion_sec,
                "root_delay_sec": self._summary.root_delay_sec,
                "root_dispersion_sec": self._summary.root_dispersion_sec,
                "stratum": self._summary.stratum,
                "last_sync_at": self._summary.last_sync_at,
                "sync_attempt_ok_count": self._summary.sync_attempt_ok_count,
                "sync_attempt_fail_count": self._summary.sync_attempt_fail_count,
                "uptime_sec": self.uptime_sec(),
                "servers": [
                    {
                        "host": st.host,
                        "ok": st.ok,
                        "last_checked_at": st.last_checked_at,
                        "offset_sec": st.offset_sec,
                        "delay_sec": st.delay_sec,
                        "stratum": st.stratum,
                        "error": st.error,
                    }
                    for st in self._server_status.values()
                ],
            }

    def now_ntp(self) -> Tuple[int, int]:
        """
        Текущее "идеальное" время в формате (seconds, fraction) относительно NTP epoch.
        """
        with self._lock:
            unix_time = self._base_unix + (time.monotonic() - self._base_mono)
            sec, frac = unix_to_ntp_timestamp(unix_time)
            return sec, frac

    def reference_ntp_bytes(self) -> bytes:
        with self._lock:
            return self._reference_ntp

    def root_delay_ntp_units(self) -> int:
        with self._lock:
            return self._root_delay_ntp_units

    def root_dispersion_ntp_units(self) -> int:
        with self._lock:
            return self._root_dispersion_ntp_units

    def stratum_for_reply(self) -> int:
        with self._lock:
            if self._summary.synced:
                return self._summary.stratum
            return self.ntp_stratum_bad

