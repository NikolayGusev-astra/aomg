"""Политика перезапуска stdio-детей (ADR-0004).

Ядро регрессии — баг-репорт пользователя «каждые 30 секунд краш».
Корневая причина в logs/weather.log установленной сборки:
`ModuleNotFoundError: No module named 'mcp.server.fastmcp' (mcp 2.x)` —
ребёнок умирает на initialize, а watchdog рестартует безусловно, вечно.

Инварианты: I1 (число Popen конечно), I2 (успех сбрасывает цепь),
I3 (ручной рестарт сбрасывает), I4 (HTTP не перезапускается).
"""
import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from aomg.config import Config, Group, ServerSpec
from aomg.health import Health
from aomg.supervisor import (MAX_FAILS, RESTART_BACKOFF, Supervisor,
                             spawn_counter)


DEAD = "no-such-binary-aomg-test.exe"


class _FakePopen:
    """Процесс, который умирает сразу; считает спавны."""

    instances: list = []

    def __init__(self, *a, **kw):
        self.pid = 4242
        self.args = a
        _FakePopen.instances.append(self)

    def poll(self):
        return 1          # сразу мёртв

    def terminate(self):
        pass

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 1


def _child_spawns(name: str = "dead") -> int:
    """Спавны ребёнка по имени сервера.

    `spawn_counter` в supervisor пишет имя сервера при каждом Popen
    (строка spawn_counter.append(self.spec.name)), поэтому прокси-
    процесс туда не попадает и метрика не удваивается.
    """
    return sum(1 for entry in spawn_counter if entry == name)


@pytest.fixture
def dead_stdio(tmp_path, monkeypatch):
    """Ребёнок спавнится и сразу умирает — как в баг-репорте.

    Команда должна СУЩЕСТВОВАТЬ на диске: иначе срабатывает
    предварительная проверка команды, рестарт не происходит вовсе и
    проверять нечего. Существование файла + подменённый Popen = чистая
    модель «процесс упал на старте».
    """
    monkeypatch.setattr("aomg.supervisor.subprocess.Popen", _FakePopen)
    monkeypatch.setattr("aomg.supervisor.VENV", tmp_path)
    spawn_counter.clear()
    _FakePopen.instances.clear()
    exe = tmp_path / "dead-mcp.exe"
    exe.write_text("x", encoding="utf-8")
    cfg = Config(gateway_port=9393, groups={"direct": Group(name="Дом")})
    spec = ServerSpec(name="dead", kind="stdio", command=str(exe))
    cfg.servers["dead"] = spec
    sup = Supervisor(cfg)
    sup.add("dead", spec)
    assert _child_spawns() == 1, "фикстура обязана дать ровно один спавн"
    # дальше считаем ТОЛЬКО рестарты: счётчик сброшен после add()
    spawn_counter.clear()
    yield sup, cfg
    spawn_counter.clear()


def _watch_ticks(sup, count, now=None):
    """Прогоняем count итераций политики с монотонным временем."""
    t = now if now is not None else 1_000.0
    for _ in range(count):
        sup.watch_tick(t)
        t += 3600.0          # каждый «тик» — через час, backoff точно прошёл
    return t


# ---------- I1: рестарт-цикл конечен ----------

def test_i1_restart_count_bounded_by_max_fails(dead_stdio):
    """Ключевой тест: бесконечного респауна больше нет."""
    sup, cfg = dead_stdio
    _watch_ticks(sup, 50)
    assert _child_spawns() <= MAX_FAILS, \
        f"рестартов {_child_spawns()} при MAX_FAILS={MAX_FAILS} — цикл не конечен"


def test_i1_circuit_opens_and_state_is_down(dead_stdio):
    sup, _cfg = dead_stdio
    _watch_ticks(sup, MAX_FAILS + 2)
    h = sup.healths["dead"]
    assert h.state == "down", "после серии падений состояние — down"
    assert "авторестарт" in (h.error or "").lower() or \
           "попыт" in (h.error or "").lower(), \
        f"в error должна быть причина, а не пустота: {h.error!r}"


def test_i1_down_error_contains_reason_from_log(dead_stdio, tmp_path):
    """Пользователь спросил 'а почему падает' — ответ в error из лога."""
    sup, _cfg = dead_stdio
    from aomg import supervisor as sv
    sv.set_logs_dir(tmp_path / "logs")
    (tmp_path / "logs" / "dead.log").write_text(
        "ModuleNotFoundError: No module named 'mcp.server.fastmcp'\n",
        encoding="utf-8")
    _watch_ticks(sup, MAX_FAILS + 2)
    err = sup.healths["dead"].error or ""
    assert "mcp.server.fastmcp" in err, \
        f"причина из лога обязана попасть в UI: {err!r}"
    sv.set_logs_dir(None)


# ---------- backoff ----------

def test_backoff_delays_second_restart(dead_stdio):
    """Второй рестарт не мгновенный: пауза растёт, потолок 60 с."""
    from aomg.supervisor import RESTART_BACKOFF
    assert RESTART_BACKOFF[0] == 0, "первый рестарт — сразу"
    assert list(RESTART_BACKOFF) == sorted(RESTART_BACKOFF), "backoff растёт"
    assert RESTART_BACKOFF[-1] <= 60


def test_no_restart_before_backoff_elapsed(dead_stdio):
    sup, _cfg = dead_stdio
    t0 = 1_000.0
    sup.watch_tick(t0)                       # попытка 1, рестарт сразу
    n_after_first = _child_spawns()
    assert n_after_first == 1, "первый рестарт происходит сразу"
    sup.watch_tick(t0 + 1)                   # рано для попытки 2
    assert _child_spawns() == n_after_first, \
        "рестарт проигнорирован backoff-паузой"
    sup.watch_tick(t0 + RESTART_BACKOFF[1] + 1)
    assert _child_spawns() > n_after_first, \
        "после паузы рестарт обязан состояться"


# ---------- I2: успех сбрасывает цепь ----------

def test_i2_success_resets_failure_chain(dead_stdio):
    sup, _cfg = dead_stdio
    sup.watch_tick(1_000.0)
    sup.watch_tick(1_000.0 + RESTART_BACKOFF[1] + 1)
    assert sup.fails["dead"] >= 1
    sup.note_alive("dead", 1_100.0)         # probe() == ok
    assert sup.fails["dead"] == 0, "оживший сервер получает полный запас попыток"
    assert sup.healths["dead"].state == "ok", \
        "оживший сервер обязан светиться зелёным, а не «перезапуск»"


def test_i2_backoff_uses_current_fail_count(dead_stdio):
    sup, _cfg = dead_stdio
    sup.fails["dead"] = 2
    assert sup.backoff_for("dead") == RESTART_BACKOFF[2]
    sup.fails["dead"] = 0
    assert sup.backoff_for("dead") == RESTART_BACKOFF[0]


# ---------- I3: ручной рестарт из панели ----------

def test_i3_manual_restart_resets_and_closes_circuit(dead_stdio):
    sup, _cfg = dead_stdio
    _watch_ticks(sup, MAX_FAILS + 2)
    assert sup.healths["dead"].state == "down"
    sup.restart("dead", manual=True)        # кнопка «Перезапустить»
    assert sup.fails["dead"] == 0
    assert sup.healths["dead"].state == "reconnecting", \
        "после ручного рестарта сервер снова в работе"


def test_i3_manual_restart_of_unknown_server_is_noop(dead_stdio):
    sup, _cfg = dead_stdio
    sup.restart("nope", manual=True)


# ---------- I4: HTTP не перезапускается ----------

def test_i4_http_server_never_auto_restarted(monkeypatch):
    monkeypatch.setattr("aomg.supervisor.subprocess.Popen", _FakePopen)
    spawn_counter.clear()
    cfg = Config(gateway_port=9392, groups={"direct": Group(name="Дом")})
    spec = ServerSpec(name="remote", kind="http", url="http://x.invalid/mcp")
    cfg.servers["remote"] = spec
    sup = Supervisor(cfg)
    sup.add("remote", spec)
    for _ in range(20):
        sup.watch_tick(1_000.0 + _ * 3600.0)
    assert spawn_counter == [], "у HTTP-сервера нет процесса — нечего рестартить"
    assert "remote" not in sup.fails


# ---------- предварительная проверка команды ----------

def test_dead_command_detected_without_spawn(monkeypatch, tmp_path):
    """Нет бинаря -> down сразу, без единого Popen (7-я проверка фазы 2)."""
    monkeypatch.setattr("aomg.supervisor.subprocess.Popen", _FakePopen)
    spawn_counter.clear()
    cfg = Config(gateway_port=9391, groups={"direct": Group(name="Дом")})
    spec = ServerSpec(name="ghost", kind="stdio",
                      command=str(tmp_path / "nope.exe"))
    cfg.servers["ghost"] = spec
    sup = Supervisor(cfg)
    sup.add("ghost", spec)
    assert spawn_counter == [], "несуществующую команду не спавним"
    h = sup.healths["ghost"]
    assert h.state == "down"
    assert "не найден" in (h.error or "").lower() or \
           "not found" in (h.error or "").lower(), h.error


def test_existing_command_is_spawned(monkeypatch, tmp_path):
    """Проверка не даёт false positive: реальный файл спавнится."""
    monkeypatch.setattr("aomg.supervisor.subprocess.Popen", _FakePopen)
    exe = tmp_path / "real-mcp.exe"
    exe.write_text("x", encoding="utf-8")
    spawn_counter.clear()
    cfg = Config(gateway_port=9390, groups={"direct": Group(name="Дом")})
    spec = ServerSpec(name="real", kind="stdio", command=str(exe))
    cfg.servers["real"] = spec
    sup = Supervisor(cfg)
    sup.add("real", spec)
    assert len(spawn_counter) == 1
    assert sup.healths["real"].state == "reconnecting"


# ---------- watchdog: один плохой сервер не роняет цикл ----------

def test_watch_once_survives_dead_and_live_servers(dead_stdio):
    """Упавший сосед не должен мешать живому серверу в том же цикле."""
    sup, _cfg = dead_stdio
    live = ServerSpec(name="live", kind="http", url="http://127.0.0.1:9/mcp")
    sup.add("live", live)

    def probe(name):
        if name == "live":
            return "ok", 3, None
        return "down", 0, "connection closed"

    sup.watch_tick(1_000.0, probe=probe)
    sup.watch_tick(1_000.0 + 3600.0, probe=probe)
    assert sup.healths["live"].state == "ok", \
        "упавший сосед не должен мешать живому серверу"
    assert sup.healths["dead"].state in ("reconnecting", "down")


def test_watchdog_module_exposes_watch_once():
    """Структурный инвариант: цикл вынесен в пошаговый watch_once."""
    from aomg import watchdog as wd
    assert hasattr(wd, "watch_once"), \
        "нужен пошаговый watch_once для тестируемости политики"
    assert hasattr(wd, "watch_loop")
