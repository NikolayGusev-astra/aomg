"""Выбор порта гейтвея (ADR-0006, инварианты I1–I3).

Проблема: `gateway_port: 9300` жёстко попадал и в автосозданный конфиг,
и в uvicorn. На занятом порту приложение молча умирало со стеком
uvicorn и нечитаемым `[Errno 10048]` от консольного кодировщика.

Разделение двух случаев:
- порт выбран автоматически (нет в конфиге) — подставляем свободный,
  пишем факт в лог, приложение работает;
- порт задан пользователем и занят — молча менять нельзя: агент уже
  настроен на этот адрес. Даём диагностику и ненулевой код выхода.
"""
from __future__ import annotations

import socket


class PortUnavailable(Exception):
    """Базовый класс: порт занят и подобрать нечем."""


class GatewayPortBusy(PortUnavailable):
    """Явно заданный в конфиге порт занят другим процессом."""


class NoFreePort(PortUnavailable):
    """Свободный порт не нашёлся за отведённое число попыток."""


def is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    """Можно ли занять порт прямо сейчас (bind без listen)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        # SO_EXCLUSIVEADDRUSE на Windows не даёт проверить занятость
        # чужого сокета в TIME_WAIT «на ours».
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        except (AttributeError, OSError):
            pass
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def resolve_gateway_port(preferred: int, host: str = "127.0.0.1",
                         explicit: bool = False, attempts: int = 20) -> int:
    """Вернуть порт для гейтвея.

    explicit=True — порт выбрал пользователь (записан в конфиге).
    Тогда занятый порт считается ошибкой конфигурации, а не поводом
    молча увести агента на другой адрес.
    """
    if is_port_free(preferred, host):
        return preferred
    if explicit:
        raise GatewayPortBusy(
            f"порт {preferred} в config.yaml занят другим процессом. "
            f"Освободите его или укажите другой gateway_port — AOMG не "
            f"меняет адрес молча, иначе агент не найдёт шлюз по старой "
            f"ссылке.")
    last: Exception | None = None
    for _ in range(attempts):
        try:
            cand = _free_port()
        except OSError as e:          # порты кончились на стороне ОС
            last = e
            continue
        if cand != preferred and is_port_free(cand, host):
            return cand
    raise NoFreePort(
        f"не удалось найти свободный порт для гейтвея: проверено "
        f"{attempts} кандидатов, {preferred} занят"
        + (f" ({last})" if last else "")
        + ". Закройте приложения, слушающие 127.0.0.1, и запустите снова.")
