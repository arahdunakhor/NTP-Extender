import asyncio
import struct
import time
from typing import Optional, Tuple

from .clock_state import ClockState
from .ntp_utils import NTP_PACKET_LEN


class NtpUdpProtocol(asyncio.DatagramProtocol):
    """
    Минимальный NTP-ответчик.

    Мы не делаем полный NTP daemon (leap/clock discipline),
    но раздаём клиентам корректированные timestamp'ы на основе рассчитанного offset.
    """

    def __init__(self, clock_state: ClockState) -> None:
        self._clock = clock_state
        self._transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr) -> None:
        if len(data) < NTP_PACKET_LEN:
            return

        # Поле "Transmit Timestamp" запроса клиента находится в байтах [40:48].
        client_transmit_ts = data[40:48]

        receive_sec, receive_frac = self._clock.now_ntp()
        # Немного позже фиксируем transmit, чтобы поля receive/transmit отличались минимально.
        transmit_sec, transmit_frac = self._clock.now_ntp()

        ref_bytes = self._clock.reference_ntp_bytes()
        root_delay_units = self._clock.root_delay_ntp_units()
        root_dispersion_units = self._clock.root_dispersion_ntp_units()
        stratum = self._clock.stratum_for_reply()

        # LI=0, VN=3, Mode=4 (server)
        # VN=3 чаще встречается в практике; так выше совместимость с NTP-клиентами.
        li_vn_mode = (0 << 6) | (3 << 3) | 4

        # Poll: экспонента двоичного логарифма интервала опроса (в т.ч. клиентские значения).
        poll = 4  # 16 сек (условно)
        # Precision: -20 => шаг точности 2^-20 секунды (типично).
        precision = (-20) & 0xFF

        # Reference identifier: упрощённо.
        ref_id = b"\x00\x00\x00\x00"

        response = bytearray(NTP_PACKET_LEN)
        response[0] = li_vn_mode
        response[1] = int(stratum) & 0xFF
        response[2] = int(poll) & 0xFF
        response[3] = int(precision) & 0xFF
        response[4:8] = struct.pack("!I", root_delay_units & 0xFFFFFFFF)
        response[8:12] = struct.pack("!I", root_dispersion_units & 0xFFFFFFFF)
        response[12:16] = ref_id

        # Reference timestamp (фиксируем как reference point последней синхронизации).
        response[16:24] = ref_bytes
        # Originate timestamp: копируем transmit из запроса клиента.
        response[24:32] = client_transmit_ts

        response[32:40] = struct.pack("!II", receive_sec, receive_frac)
        response[40:48] = struct.pack("!II", transmit_sec, transmit_frac)

        if self._transport is not None:
            self._transport.sendto(response, addr)

    def error_received(self, exc: Exception) -> None:
        # Здесь просто логирование (в текущей версии без глобального логгера).
        _ = exc

