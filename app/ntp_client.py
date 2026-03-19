from typing import Dict

import ntplib


_client = ntplib.NTPClient()


def query_ntp_server(host: str, timeout_sec: float) -> Dict[str, object]:
    """
    Делает запрос NTP к серверу и возвращает смещение относительно локального времени.
    ntplib использует system time для вычисления offset — для нас это достаточно,
    т.к. мы фиксируем offset и дальше раздаём скорректированные timestamp'ы в ответах NTP.
    """
    resp = _client.request(host, version=4, timeout=timeout_sec)
    return {
        "ok": True,
        "offset_sec": float(resp.offset),
        "delay_sec": float(resp.delay),
        "stratum": getattr(resp, "stratum", None),
        "server_time": getattr(resp, "tx_time", None),
    }

