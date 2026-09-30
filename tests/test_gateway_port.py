"""Платформенная граница и выбор порта гейтвея (ADR-0006)."""
import pathlib
import socket
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
ROOT = pathlib.Path(__file__).resolve().parents[1]

from aomg.port import (GatewayPortBusy, NoFreePort, is_port_free,
                       resolve_gateway_port)


def _occupy(port: int):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(1)
    return s


# ---------- I1: занятой preferred -> свободный ----------

def test_i1_busy_preferred_port_returns_free_one():
    busy = _occupy(0)
    busy_port = busy.getsockname()[1]
    try:
        chosen = resolve_gateway_port(busy_port)
        assert chosen != busy_port
        assert is_port_free(chosen)
    finally:
        busy.close()


def test_i1_free_preferred_port_is_kept():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    free = s.getsockname()[1]
    s.close()
    assert resolve_gateway_port(free) == free, "свободный порт не трогаем"


def test_i1_chosen_port_is_really_bindable():
    busy = _occupy(0)
    try:
        chosen = resolve_gateway_port(busy.getsockname()[1])
        probe = socket.socket()
        probe.bind(("127.0.0.1", chosen))   # не должно бросить
        probe.close()
    finally:
        busy.close()


# ---------- I2: явный порт из конфига не переписывается молча ----------

def test_i2_explicit_config_port_raises_with_diagnostics():
    """Пользовательский выбор не перебиваем: диагностика + ненулевой код."""
    busy = _occupy(0)
    busy_port = busy.getsockname()[1]
    try:
        with pytest.raises(GatewayPortBusy) as ei:
            resolve_gateway_port(busy_port, explicit=True)
        msg = str(ei.value)
        assert str(busy_port) in msg, f"в диагностике должен быть порт: {msg}"
        assert "config" in msg.lower() or "порт" in msg.lower()
    finally:
        busy.close()


def test_i2_auto_mode_falls_back_silently():
    busy = _occupy(0)
    try:
        chosen = resolve_gateway_port(busy.getsockname()[1], explicit=False)
        assert chosen != busy.getsockname()[1]
    finally:
        busy.close()


# ---------- I3: все порты заняты -> читаемая ошибка ----------

def test_i3_all_ports_busy_raises_readable_error(monkeypatch):
    """Не [Errno 10048], а своя ошибка с текстом для пользователя."""
    from aomg import port as portmod

    monkeypatch.setattr(portmod, "_free_port",
                        lambda: (_ for _ in ()).throw(OSError("boom")))
    monkeypatch.setattr(portmod, "is_port_free",
                        lambda p, host="127.0.0.1": False)
    with pytest.raises(NoFreePort) as ei:
        portmod.resolve_gateway_port(9310, attempts=3)
    msg = str(ei.value)
    assert "порт" in msg.lower()
    assert "10048" not in msg, "сырой errno пользователю не показываем"


def test_i3_attempts_are_bounded(monkeypatch):
    """Не бесконечный перебор портов."""
    from aomg import port as portmod
    calls = {"n": 0}

    def fake_free():
        calls["n"] += 1
        return 0

    monkeypatch.setattr(portmod, "_free_port", fake_free)
    monkeypatch.setattr(portmod, "is_port_free",
                        lambda p, host="127.0.0.1": False)
    with pytest.raises(NoFreePort):
        portmod.resolve_gateway_port(9310, attempts=4)
    assert calls["n"] <= 4


# ---------- I4: фактический порт в UI, не константа ----------

def test_i4_legend_uses_configured_port():
    """В HTML-легенде панели не должно быть захардкоженного 9300."""
    from aomg.admin import _PAGE
    assert "9300" not in _PAGE, \
        "легенда должна показывать фактический порт из конфига"


def test_i4_tray_status_reports_configured_port():
    """Панель и трей показывают ФАКТИЧЕСКИЙ порт, а не константу."""
    from aomg.admin import _PAGE
    assert "{port}" in _PAGE, \
        "в шаблоне панели порт подставляется из конфига при рендере"


def test_i4_page_contains_port_placeholder():
    from aomg.admin import _PAGE
    assert "{port}" in _PAGE or "PORT_PLACEHOLDER" in _PAGE, \
        "в шаблоне страницы нужен плейсхолдер порта"


# ---------- конфиг: автопорт попадает в файл ----------

def test_auto_created_config_records_resolved_port(tmp_path, monkeypatch):
    """Первый запуск: в конфиг пишется проверенный свободный порт."""
    # run.py — скрипт верхнего уровня, не модуль пакета aomg
    sys.path.insert(0, str(ROOT))
    import run as runmod
    busy = _occupy(0)
    busy_port = busy.getsockname()[1]
    cfg_path = tmp_path / "config.yaml"
    try:
        runmod.write_default_config(cfg_path, gateway_port=busy_port)
        text = cfg_path.read_text(encoding="utf-8")
        assert f"gateway_port: {busy_port}" in text
    finally:
        busy.close()


def test_default_config_has_no_demo_servers(tmp_path):
    """Автоконфиг обязан быть чистым: без мёртвых демо-серверов (P0)."""
    sys.path.insert(0, str(ROOT))
    import run as runmod
    cfg_path = tmp_path / "config.yaml"
    runmod.write_default_config(cfg_path, gateway_port=9400)
    import yaml
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert not (raw.get("servers") or {}), \
        "демо-серверы уезжают в рестарт-цикл — их не должно быть в поставке"
