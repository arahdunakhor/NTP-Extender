import math
import struct
from typing import Tuple


NTP_UNIX_EPOCH_OFFSET = 2208988800  # Разница между Unix epoch и NTP epoch (1900-01-01)
NTP_PACKET_LEN = 48


def clamp_u32(value: int) -> int:
    return max(0, min(0xFFFFFFFF, value))


def unix_to_ntp_timestamp(unix_time: float) -> Tuple[int, int]:
    """
    Конвертирует время Unix (секунды) в NTP timestamp (seconds, fraction).
    seconds: 32-bit unsigned, fraction: 32-bit unsigned (дробная часть фиксированной точки).
    """
    if not math.isfinite(unix_time):
        unix_time = 0.0
    ntp_time = unix_time + NTP_UNIX_EPOCH_OFFSET
    sec = int(ntp_time)
    frac = int((ntp_time - sec) * (1 << 32)) & 0xFFFFFFFF
    return clamp_u32(sec), clamp_u32(frac)


def pack_ntp_timestamp(seconds: int, fraction: int) -> bytes:
    return struct.pack("!II", clamp_u32(seconds), clamp_u32(fraction))


def ntp_timestamp_bytes_to_tuple(ts_bytes: bytes) -> Tuple[int, int]:
    seconds, fraction = struct.unpack("!II", ts_bytes)
    return seconds, fraction


def ntp_timestamp_tuple_to_str(seconds: int, fraction: int) -> str:
    # Для логов: показываем seconds как Unix приблизительно (без строгой обратимости).
    unix_approx = seconds - NTP_UNIX_EPOCH_OFFSET + (fraction / (1 << 32))
    if not math.isfinite(unix_approx):
        return "NaN"
    return f"{unix_approx:.6f} unix(s)"

