import json
from dataclasses import dataclass, asdict
from typing import List


@dataclass
class AppConfig:
    # Часовой пояс для отображения в веб-интерфейсе (NTP работает в UTC).
    timezone: str = "Europe/Moscow"

    # Список внешних NTP-серверов (DNS или IP), с которыми выполняется синхронизация.
    external_servers: List[str] = None

    # Интервал фоновой синхронизации (секунды).
    sync_interval_sec: int = 300

    # Таймаут запроса к NTP-серверу (секунды).
    sync_timeout_sec: float = 4.0

    # Максимальная допустимая "рассеянность" (дисперсия) offset'ов (секунды) для признания синхронизации здоровой.
    max_dispersion_sec: float = 5.0

    # Минимум успешных источников для расчёта состояния часов.
    min_successful_servers: int = 1

    # Какой NTP-порт отдавать клиентам во внутренней сети.
    ntp_port: int = 123

    # IP хоста, который клиенты внутренней сети будут использовать как NTP endpoint.
    # В Docker обычно это IP машины/сервера, где запущен контейнер, а не IP контейнера.
    advertised_ip: str = ""

    # Привязка NTP-сервера (в контейнере обычно 0.0.0.0).
    ntp_bind: str = "0.0.0.0"

    # Web-порт (для FastAPI/uvicorn).
    web_port: int = 12312

    # Настройки web для привязки.
    web_host: str = "0.0.0.0"

    def __post_init__(self) -> None:
        if self.external_servers is None:
            # Небольшой стартовый набор. В проде удобнее добавить свои источники.
            self.external_servers = [
                "time.google.com",
                "time.cloudflare.com",
                "pool.ntp.org",
            ]

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @staticmethod
    def from_dict(data: dict) -> "AppConfig":
        cfg = AppConfig()
        for k, v in data.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
        return cfg

