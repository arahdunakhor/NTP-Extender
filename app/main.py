import asyncio
import glob
import hashlib
import ipaddress
import json
import logging
import logging.handlers
import os
import secrets
import socket
import statistics
import time
from collections import deque
from dataclasses import asdict
from typing import Any, Deque, Dict, List, Optional

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .clock_state import ClockState
from .config import AppConfig
from .ntp_client import query_ntp_server
from .ntp_server import NtpUdpProtocol
from .storage import load_json, save_json_atomic

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]


DATA_DIR = os.environ.get("DATA_DIR", "/data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
LOG_PATH = os.path.join(DATA_DIR, "app.log")
AUTH_PATH = os.path.join(DATA_DIR, "auth.json")
LOG_RETENTION_DAYS = 7
LOG_MAX_TOTAL_BYTES = 50 * 1024 * 1024  # 50 MB


templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "web"))

logger = logging.getLogger("ntp-extender")


class RingBufferHandler(logging.Handler):
    def __init__(self, buffer: Deque[str], *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        self._buffer.append(msg)


def _cleanup_log_files() -> None:
    """
    Удаляем старые и избыточные лог-файлы:
    - старше LOG_RETENTION_DAYS
    - при превышении LOG_MAX_TOTAL_BYTES удаляем самые старые.
    """
    try:
        now = time.time()
        max_age_sec = LOG_RETENTION_DAYS * 24 * 3600
        files = []
        for path in glob.glob(os.path.join(DATA_DIR, "app.log*")):
            if not os.path.isfile(path):
                continue
            try:
                stat = os.stat(path)
                files.append({"path": path, "mtime": stat.st_mtime, "size": stat.st_size})
            except Exception:
                continue

        # Удаляем по возрасту.
        for f in files:
            if (now - f["mtime"]) > max_age_sec:
                try:
                    os.remove(f["path"])
                except Exception:
                    pass

        # Перечитываем и ограничиваем суммарный объём.
        files = []
        for path in glob.glob(os.path.join(DATA_DIR, "app.log*")):
            if not os.path.isfile(path):
                continue
            try:
                stat = os.stat(path)
                files.append({"path": path, "mtime": stat.st_mtime, "size": stat.st_size})
            except Exception:
                continue

        total = sum(f["size"] for f in files)
        if total <= LOG_MAX_TOTAL_BYTES:
            return

        # Удаляем самые старые файлы, пока не уложимся в лимит.
        files.sort(key=lambda x: x["mtime"])
        for f in files:
            if total <= LOG_MAX_TOTAL_BYTES:
                break
            try:
                os.remove(f["path"])
                total -= f["size"]
            except Exception:
                pass
    except Exception:
        # Очистка логов не должна ломать приложение.
        pass


async def _run_sync_once(cfg: AppConfig, clock: ClockState, log_buffer: Deque[str]) -> Dict[str, Any]:
    """
    Одна попытка синхронизации:
    - опрашиваем все внешние NTP сервера
    - выбираем медианный offset
    - решаем, "здоровы" ли часы (по дисперсии)
    - обновляем clock_state
    """
    loop_start = time.time()
    hosts = [h.strip() for h in (cfg.external_servers or []) if h.strip()]
    if not hosts:
        clock.apply_successful_sync(
            chosen_offset_sec=0.0,
            dispersion_sec=None,
            root_delay_sec=None,
            root_dispersion_sec=None,
            synced=False,
            stratum=clock.ntp_stratum_bad,  # type: ignore[attr-defined]
            last_sync_at=time.time(),
            attempt_ok=False,
        )
        return {
            "ok": False,
            "reason": "Список серверов пуст",
            "duration_sec": time.time() - loop_start,
        }

    # Параллельно опрашиваем NTP.
    async def check_one(host: str) -> Dict[str, Any]:
        t0 = time.time()
        try:
            result = await asyncio.to_thread(query_ntp_server, host, cfg.sync_timeout_sec)
            result.update(
                ok=True,
                last_checked_at=time.time(),
                error=None,
                duration_sec=time.time() - t0,
            )
            return result
        except Exception as e:
            return {
                "ok": False,
                "offset_sec": None,
                "delay_sec": None,
                "stratum": None,
                "server_time": None,
                "last_checked_at": time.time(),
                "error": str(e),
                "duration_sec": time.time() - t0,
            }

    results = await asyncio.gather(*(check_one(h) for h in hosts), return_exceptions=False)

    successful = [r for r in results if r.get("ok") is True and r.get("offset_sec") is not None]
    for host, r in zip(hosts, results):
        clock.update_server_check(host, r)

    if len(successful) < cfg.min_successful_servers:
        logger.warning(
            "Синхронизация не удалась: успешных источников=%s (минимум=%s)",
            len(successful),
            cfg.min_successful_servers,
        )
        clock.apply_successful_sync(
            chosen_offset_sec=0.0,
            dispersion_sec=None,
            root_delay_sec=None,
            root_dispersion_sec=None,
            synced=False,
            stratum=16,
            last_sync_at=time.time(),
            attempt_ok=False,
        )
        return {
            "ok": False,
            "reason": "Недостаточно успешных источников",
            "success_count": len(successful),
            "failure_count": len(results) - len(successful),
            "duration_sec": time.time() - loop_start,
        }

    offsets = [float(r["offset_sec"]) for r in successful]  # type: ignore[arg-type]
    delays = [float(r.get("delay_sec") or 0.0) for r in successful]  # type: ignore[arg-type]

    chosen_offset = float(statistics.median(offsets))

    deviations = [abs(o - chosen_offset) for o in offsets]
    dispersion = float(statistics.median(deviations)) if deviations else 0.0

    synced = dispersion <= float(cfg.max_dispersion_sec)
    chosen_stratum = 1 if synced else 16

    root_delay = float(statistics.median(delays)) if delays else None
    root_dispersion = dispersion if synced else dispersion

    logger.info(
        "Синхронизация выполнена: chosen_offset=%.6f s dispersion=%.6f s synced=%s sources=%s",
        chosen_offset,
        dispersion,
        synced,
        len(successful),
    )
    clock.apply_successful_sync(
        chosen_offset_sec=chosen_offset,
        dispersion_sec=dispersion,
        root_delay_sec=root_delay,
        root_dispersion_sec=root_dispersion,
        synced=synced,
        stratum=chosen_stratum,
        last_sync_at=time.time(),
        attempt_ok=True,
    )

    return {
        "ok": True,
        "synced": synced,
        "chosen_offset_sec": chosen_offset,
        "dispersion_sec": dispersion,
        "success_count": len(successful),
        "failure_count": len(results) - len(successful),
        "duration_sec": time.time() - loop_start,
    }


def _configure_logging() -> Deque[str]:
    os.makedirs(DATA_DIR, exist_ok=True)
    log_buffer: Deque[str] = deque(maxlen=500)

    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%Y-%m-%d %H:%M:%S")

    logger.setLevel(logging.INFO)
    # Чтобы не дублировать handlers при перезапуске в dev-режиме.
    if not logger.handlers:
        ring_handler = RingBufferHandler(log_buffer)
        ring_handler.setFormatter(formatter)
        ring_handler.setLevel(logging.INFO)

        logger.addHandler(ring_handler)

        # Ротация по дню + ограничение количества файлов.
        file_handler = logging.handlers.TimedRotatingFileHandler(
            LOG_PATH,
            when="midnight",
            interval=1,
            backupCount=LOG_RETENTION_DAYS,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.INFO)
        logger.addHandler(file_handler)

    _cleanup_log_files()
    return log_buffer


def _validate_timezone(tz: str) -> None:
    if not tz or not isinstance(tz, str):
        raise ValueError("Некорректный часовой пояс")
    if ZoneInfo is None:
        return
    # ZoneInfo бросит исключение при неверном имени.
    ZoneInfo(tz)


def _detect_local_ip() -> str:
    """
    Пытаемся определить IP хоста в локальной сети.
    Используется как fallback, когда UI открыт через localhost.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            local_ip = sock.getsockname()[0]
        finally:
            sock.close()
        return local_ip
    except Exception:
        return "127.0.0.1"


def _resolve_client_ntp_ip(request: Request) -> str:
    host = (request.url.hostname or "").strip()
    if not host:
        return _detect_local_ip()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return _detect_local_ip()
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    try:
        resolved = socket.gethostbyname(host)
        return resolved or host
    except Exception:
        return host


def _validate_ntp_server_target(host: str) -> None:
    """
    Базовая защита от опасных/локальных целей для NTP-опроса.
    Нужна для снижения риска SSRF и сканирования внутренних сервисов.
    """
    host_clean = (host or "").strip()
    if not host_clean:
        raise ValueError("Пустой адрес сервера")

    host_lower = host_clean.lower()
    if "://" in host_lower:
        raise ValueError("Укажите только DNS-имя или IP без схемы")

    if host_lower in {"localhost", "host.docker.internal", "0.0.0.0"}:
        raise ValueError("Локальные адреса запрещены")

    # Если указан literal IP — блокируем не-глобальные адреса.
    try:
        ip = ipaddress.ip_address(host_clean)
        if not ip.is_global:
            raise ValueError("Разрешены только глобальные IP адреса")
        return
    except ValueError:
        # Это не literal IP, возможно DNS-имя.
        pass

    # Для DNS-имён пытаемся разрешить IP и проверяем, что они глобальные.
    try:
        infos = socket.getaddrinfo(host_clean, 123, family=socket.AF_UNSPEC, type=socket.SOCK_DGRAM)
    except Exception as e:
        raise ValueError(f"Не удалось разрешить DNS имя: {e}")

    resolved_ips = set()
    for item in infos:
        sockaddr = item[4]
        if not sockaddr:
            continue
        resolved_ips.add(sockaddr[0])

    if not resolved_ips:
        raise ValueError("DNS имя не разрешилось в IP")

    for ip_str in resolved_ips:
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if not ip.is_global:
            raise ValueError(f"DNS имя указывает на неглобальный IP: {ip_str}")


app = FastAPI(title="NTP Extender")
MAX_EXTERNAL_SERVERS = 10
SESSION_SECRET = os.environ.get("SESSION_SECRET", "change-me-session-secret")

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="ntp_extender_session",
    same_site="lax",
    https_only=False,
)


def _hash_password(password: str, salt_hex: str) -> str:
    salt = bytes.fromhex(salt_hex)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return digest.hex()


def _default_auth_data() -> Dict[str, Any]:
    salt = secrets.token_hex(16)
    return {
        "username": "admin",
        "salt": salt,
        "password_hash": _hash_password("admin", salt),
    }


def _load_auth_data() -> Dict[str, Any]:
    data = load_json(AUTH_PATH, _default_auth_data)
    if not isinstance(data, dict):
        data = _default_auth_data()
    if "username" not in data or "salt" not in data or "password_hash" not in data:
        data = _default_auth_data()
    save_json_atomic(AUTH_PATH, data)
    return data


def _verify_password(password: str, auth_data: Dict[str, Any]) -> bool:
    try:
        expected = str(auth_data["password_hash"])
        salt = str(auth_data["salt"])
        actual = _hash_password(password, salt)
        return secrets.compare_digest(expected, actual)
    except Exception:
        return False


def _is_authenticated(request: Request) -> bool:
    return request.session.get("user") == "admin"


def _ensure_authenticated(request: Request) -> None:
    if not _is_authenticated(request):
        raise HTTPException(status_code=401, detail="Требуется авторизация")


async def _restart_ntp_listener(new_port: int) -> None:
    """
    Перезапускает UDP NTP listener на новом порту.
    Если запуск на новом порту не удался, пытается откатиться на старый.
    """
    if new_port < 1 or new_port > 65535:
        raise ValueError("Порт NTP должен быть в диапазоне 1..65535")

    cfg: AppConfig = app.state.cfg
    clock: ClockState = app.state.clock
    old_port = int(getattr(app.state, "ntp_port", cfg.ntp_port))
    if new_port == old_port:
        return

    old_transport = getattr(app.state, "ntp_transport", None)
    if old_transport is not None:
        old_transport.close()
        await asyncio.sleep(0)

    loop = asyncio.get_running_loop()
    try:
        transport, _protocol = await loop.create_datagram_endpoint(
            lambda: NtpUdpProtocol(clock),
            local_addr=(cfg.ntp_bind, int(new_port)),
        )
    except Exception as e:
        logger.exception("Не удалось поднять NTP listener на порту %s: %s", new_port, e)
        # Пытаемся восстановить старый порт.
        try:
            transport, _protocol = await loop.create_datagram_endpoint(
                lambda: NtpUdpProtocol(clock),
                local_addr=(cfg.ntp_bind, int(old_port)),
            )
            app.state.ntp_transport = transport
            app.state.ntp_port = old_port
        except Exception:
            logger.exception("Критично: не удалось восстановить старый NTP listener на порту %s", old_port)
        raise

    app.state.ntp_transport = transport
    app.state.ntp_port = int(new_port)
    logger.info("NTP listener перезапущен: %s -> %s", old_port, new_port)


@app.on_event("startup")
async def on_startup() -> None:
    log_buffer = _configure_logging()
    app.state.log_buffer = log_buffer

    cfg_dict = load_json(CONFIG_PATH, lambda: asdict(AppConfig()))
    cfg = AppConfig.from_dict(cfg_dict)
    app.state.cfg = cfg
    app.state.auth_data = _load_auth_data()

    # Разогреваем clock состояние.
    clock = ClockState(timezone=cfg.timezone)
    clock.set_server_status(cfg.external_servers or [])
    app.state.clock = clock

    # Загружаем состояние для UI (не критично; реальная шкала будет обновлена после синка).
    _ = load_json(STATE_PATH, lambda: {})

    # Запускаем NTP UDP сервер.
    loop = asyncio.get_running_loop()
    transport, _protocol = await loop.create_datagram_endpoint(
        lambda: NtpUdpProtocol(clock),
        local_addr=(cfg.ntp_bind, int(cfg.ntp_port)),
    )
    app.state.ntp_transport = transport
    app.state.ntp_port = int(cfg.ntp_port)

    stop_event = asyncio.Event()
    app.state.stop_event = stop_event

    # Проводим немедленную синхронизацию перед стартом цикла.
    try:
        await _run_sync_once(cfg, clock, log_buffer)
    except Exception:
        logger.exception("Первая синхронизация упала (NTP сервер всё равно запустится).")

    app.state.sync_task = asyncio.create_task(sync_loop())


async def sync_loop() -> None:
    cfg: AppConfig = app.state.cfg
    clock: ClockState = app.state.clock
    log_buffer: Deque[str] = app.state.log_buffer
    stop_event: asyncio.Event = app.state.stop_event

    while not stop_event.is_set():
        try:
            await _run_sync_once(cfg, clock, log_buffer)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка синхронизации (попробуем снова позже).")
        _cleanup_log_files()

        # Ждём с возможностью быстрой остановки.
        interval = max(1, int(cfg.sync_interval_sec))
        for _ in range(interval):
            if stop_event.is_set():
                break
            await asyncio.sleep(1)


@app.on_event("shutdown")
async def on_shutdown() -> None:
    stop_event: asyncio.Event = app.state.stop_event
    stop_event.set()

    task: asyncio.Task = app.state.sync_task
    task.cancel()
    try:
        await task
    except Exception:
        pass

    transport = getattr(app.state, "ntp_transport", None)
    if transport is not None:
        transport.close()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    if not _is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    cfg: AppConfig = app.state.cfg
    status: Dict[str, Any] = app.state.clock.to_api_status()
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "timezone": cfg.timezone, "status": status, "config": asdict(cfg)},
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    if _is_authenticated(request):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/api/login")
async def api_login(request: Request, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    auth_data = app.state.auth_data
    if username != str(auth_data.get("username", "admin")) or not _verify_password(password, auth_data):
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    request.session["user"] = "admin"
    return {"ok": True}


@app.post("/api/logout")
async def api_logout(request: Request) -> Dict[str, Any]:
    request.session.clear()
    return {"ok": True}


@app.get("/api/status")
async def api_status(request: Request) -> Dict[str, Any]:
    _ensure_authenticated(request)
    cfg: AppConfig = app.state.cfg
    clock: ClockState = app.state.clock
    status = clock.to_api_status()
    status["config"] = {
        "timezone": cfg.timezone,
        "external_servers": cfg.external_servers,
        "sync_interval_sec": cfg.sync_interval_sec,
        "sync_timeout_sec": cfg.sync_timeout_sec,
        "max_dispersion_sec": cfg.max_dispersion_sec,
        "ntp_port": cfg.ntp_port,
    }
    client_ip = _resolve_client_ntp_ip(request)
    status["ntp_endpoint"] = f"{client_ip}:{cfg.ntp_port}"
    status["docker_note"] = (
        "Если контейнер запущен в Docker bridge, изменение NTP порта в UI требует "
        "соответствующего проброса UDP порта на хосте."
    )
    return status


@app.post("/api/sync")
async def api_sync(request: Request) -> Dict[str, Any]:
    _ensure_authenticated(request)
    cfg: AppConfig = app.state.cfg
    clock: ClockState = app.state.clock
    log_buffer: Deque[str] = app.state.log_buffer

    try:
        result = await _run_sync_once(cfg, clock, log_buffer)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/test")
async def api_test(request: Request, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    _ensure_authenticated(request)
    servers = payload.get("servers") or []
    timeout_sec = float(payload.get("timeout_sec", app.state.cfg.sync_timeout_sec))
    if not isinstance(servers, list) or not servers:
        raise HTTPException(status_code=400, detail="`servers` должен быть непустым списком.")

    servers_clean = [str(s).strip() for s in servers if str(s).strip()]
    if not servers_clean:
        raise HTTPException(status_code=400, detail="Список серверов пуст после очистки.")
    if len(servers_clean) > MAX_EXTERNAL_SERVERS:
        raise HTTPException(
            status_code=400,
            detail=f"Допустимо не более {MAX_EXTERNAL_SERVERS} серверов для проверки.",
        )
    for host in servers_clean:
        try:
            _validate_ntp_server_target(host)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Сервер `{host}` отклонён: {e}")

    async def check(host: str) -> Dict[str, Any]:
        t0 = time.time()
        try:
            r = await asyncio.to_thread(query_ntp_server, host, timeout_sec)
            return {"host": host, "ok": True, **r, "duration_sec": time.time() - t0, "error": None}
        except Exception as e:
            return {
                "host": host,
                "ok": False,
                "offset_sec": None,
                "delay_sec": None,
                "stratum": None,
                "server_time": None,
                "duration_sec": time.time() - t0,
                "error": str(e),
            }

    results = await asyncio.gather(*(check(h) for h in servers_clean))
    return {"ok": True, "results": results}


@app.post("/api/save_config")
async def api_save_config(request: Request, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    _ensure_authenticated(request)
    cfg: AppConfig = app.state.cfg

    timezone = payload.get("timezone", cfg.timezone)
    external_servers = payload.get("external_servers", cfg.external_servers)
    sync_interval_sec = int(payload.get("sync_interval_sec", cfg.sync_interval_sec))
    sync_timeout_sec = float(payload.get("sync_timeout_sec", cfg.sync_timeout_sec))
    max_dispersion_sec = float(payload.get("max_dispersion_sec", cfg.max_dispersion_sec))
    ntp_port = int(payload.get("ntp_port", cfg.ntp_port))

    if external_servers is None:
        external_servers = []
    if not isinstance(external_servers, list):
        raise HTTPException(status_code=400, detail="`external_servers` должен быть массивом строк.")

    servers_clean = [str(s).strip() for s in external_servers if str(s).strip()]
    if not servers_clean:
        raise HTTPException(status_code=400, detail="Список внешних серверов пуст.")
    if len(servers_clean) > MAX_EXTERNAL_SERVERS:
        raise HTTPException(
            status_code=400,
            detail=f"Допустимо не более {MAX_EXTERNAL_SERVERS} внешних серверов.",
        )
    for host in servers_clean:
        try:
            _validate_ntp_server_target(host)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Сервер `{host}` отклонён: {e}")

    try:
        _validate_timezone(str(timezone))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Некорректный часовой пояс: {e}")

    cfg.timezone = str(timezone)
    cfg.external_servers = servers_clean
    cfg.sync_interval_sec = max(10, sync_interval_sec)
    cfg.sync_timeout_sec = max(1.0, sync_timeout_sec)
    cfg.max_dispersion_sec = max(0.1, max_dispersion_sec)
    if ntp_port < 1 or ntp_port > 65535:
        raise HTTPException(status_code=400, detail="NTP порт должен быть в диапазоне 1..65535.")

    old_ntp_port = int(cfg.ntp_port)
    if ntp_port != old_ntp_port:
        try:
            await _restart_ntp_listener(ntp_port)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Не удалось применить NTP порт {ntp_port}: {e}")
    cfg.ntp_port = ntp_port

    # Обновляем clock state.
    clock: ClockState = app.state.clock
    clock.set_timezone(cfg.timezone)
    clock.set_server_status(cfg.external_servers)

    # Сохраняем конфиг на volume.
    save_json_atomic(CONFIG_PATH, asdict(cfg))
    logger.info(
        "Конфиг сохранён: timezone=%s servers=%s ntp_port=%s",
        cfg.timezone,
        len(cfg.external_servers),
        cfg.ntp_port,
    )

    return {"ok": True, "saved": True}


@app.post("/api/change_password")
async def api_change_password(request: Request, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    _ensure_authenticated(request)
    current_password = str(payload.get("current_password", ""))
    new_password = str(payload.get("new_password", ""))
    auth_data = app.state.auth_data
    if not _verify_password(current_password, auth_data):
        raise HTTPException(status_code=400, detail="Текущий пароль указан неверно")
    # По требованию пользователя: не вводим ограничений сложности/длины.
    salt = secrets.token_hex(16)
    auth_data["salt"] = salt
    auth_data["password_hash"] = _hash_password(new_password, salt)
    save_json_atomic(AUTH_PATH, auth_data)
    logger.info("Пароль администратора обновлён")
    return {"ok": True}


@app.get("/api/logs")
async def api_logs(request: Request, limit: int = 200) -> Dict[str, Any]:
    _ensure_authenticated(request)
    log_buffer: Deque[str] = app.state.log_buffer
    limit = max(1, min(int(limit), 500))
    return {"ok": True, "logs": list(log_buffer)[-limit:]}

