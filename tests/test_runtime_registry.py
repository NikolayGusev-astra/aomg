"""Runtime-реестр: Supervisor — единственный владелец Health (ADR-0003).

Регрессии, которые эти тесты фиксируют (аудит сессии
20260930_100646_31e2ee, P0):

- admin/api/servers не создавал Health -> watchdog.py:80 KeyError ->
  watchdog-поток умирает навсегда после первого добавленного MCP;
- тот же KeyError в gateway.py:59 -> 500 вместо 504;
- healths — общий словарь без синхронизации между тремя потоками.

Инварианты ADR-0003: I1 (согласованность трёх словарей), I2 (нет прямого
индексирования healths[] вне Supervisor), I3 (snapshot согласован).
"""
import pathlib
import sys
import threading
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from aomg.config import Config, Group, ServerSpec
from aomg.health import Health, aggregate_state
from aomg.supervisor import Supervisor


def _http(name):
    return ServerSpec(name=name, kind="http", url=f"http://127.0.0.1:9/{name}/mcp")


def _stdio(name):
    return ServerSpec(name=name, kind="stdio", command="definitely-not-here.exe")


def _sup(*specs):
    cfg = Config(gateway_port=9399, groups={"direct": Group(name="Дом")})
    for s in specs:
        cfg.servers[s.name] = s
    return cfg, Supervisor(cfg)


# ---------- I1: атомарность add/remove ----------

def test_i1_add_creates_health_and_managed_together():
    cfg, sup = _sup()
    assert not sup.healths and not sup.managed
    sup.add("jira", _http("jira"))
    assert "jira" in sup.healths, "Health обязан появиться вместе с процессом"
    assert "jira" in sup.managed
    assert "jira" in cfg.servers


def test_i1_add_is_idempotent():
    cfg, sup = _sup()
    sup.add("jira", _http("jira"))
    h = sup.healths["jira"]
    sup.add("jira", _http("jira"))
    assert sup.healths["jira"] is h, "повторный add не заменяет Health"


def test_i1_remove_clears_all_three():
    cfg, sup = _sup(_http("jira"), _http("bitbucket"))
    sup.add("jira", _http("jira"))
    sup.add("bitbucket", _http("bitbucket"))
    sup.remove("jira")
    assert "jira" not in sup.healths
    assert "jira" not in sup.managed
    assert "jira" not in cfg.servers
    assert "bitbucket" in sup.healths, "соседний сервер не тронут"


def test_i1_remove_unknown_name_is_noop():
    _cfg, sup = _sup()
    sup.remove("nope")          # не должно бросать


def test_i1_health_returns_none_instead_of_raising():
    _cfg, sup = _sup()
    assert sup.health("nope") is None


# ---------- I3: snapshot согласован ----------

def test_i3_snapshot_lists_config_order():
    cfg, sup = _sup(_http("a"), _http("b"), _http("c"))
    sup.add("a", _http("a"))
    sup.add("b", _http("b"))
    sup.add("c", _http("c"))
    assert [n for n, _ in sup.snapshot()] == ["a", "b", "c"]


def test_i3_snapshot_is_consistent_under_concurrent_add():
    """Под локом: в срезе не бывает сервера, которого уже нет."""
    cfg, sup = _sup()
    for i in range(40):
        cfg.servers[f"s{i}"] = _http(f"s{i}")
        sup.add(f"s{i}", _http(f"s{i}"))
    stop = threading.Event()
    errors = []

    def reader():
        while not stop.is_set():
            snap = dict(sup.snapshot())
            for name, h in snap.items():
                if name not in cfg.servers:
                    errors.append(f"{name} есть в snapshot, но нет в cfg")

    t = threading.Thread(target=reader)
    t.start()
    try:
        for i in range(40, 80):
            cfg.servers[f"s{i}"] = _http(f"s{i}")
            sup.add(f"s{i}", _http(f"s{i}"))
    finally:
        stop.set()
        t.join(timeout=5)
    assert not errors, errors[:3]


def test_i3_aggregate_over_snapshot_reflects_state():
    cfg, sup = _sup(_http("a"), _http("b"))
    sup.add("a", _http("a"))
    sup.add("b", _http("b"))
    sup.healths["a"].record("ok", tools=3, ts=time.time())
    sup.healths["b"].record("down", error="boom", ts=time.time())
    assert aggregate_state([h for _, h in sup.snapshot()]) == "down"


# ---------- регрессия P0: watchdog не умирает ----------

def test_watchdog_survives_server_missing_in_healths():
    """Ядро регрессии: сервер в cfg, но нет в healths -> падение потока.

    До фикса было KeyError и мёртвый watchdog. Теперь такой сервер
    просто игнорируется итерацией, поток живёт.
    """
    from aomg.watchdog import watch_loop
    cfg = Config(gateway_port=9399, groups={"direct": Group(name="Дом")})
    ghost = _http("ghost")
    cfg.servers["ghost"] = ghost          # есть в конфиге...
    sup = Supervisor(cfg)
    sup.add("ghost", ghost)
    sup.remove("ghost")
    cfg.servers["ghost"] = ghost          # ...и вернулся в конфиг без Health

    # сервер есть в конфиге, но без Health — рассинхрон после удаления
    t = threading.Thread(target=watch_loop,
                         args=(cfg, sup, 0.2), daemon=True)
    t.start()
    time.sleep(1.0)
    assert t.is_alive(), "watchdog умер на сервере без Health — это P0-баг"


def test_watchdog_reports_state_for_managed_server(monkeypatch):
    """Живой цикл: сервер без апстрима получает state, а не исключение."""
    from aomg import watchdog as wd
    monkeypatch.setattr(wd, "probe",
                        lambda *a, **k: ("down", 0, "connection closed"))
    cfg = Config(gateway_port=9398, groups={"direct": Group(name="Дом")})
    spec = _http("dead")
    cfg.servers["dead"] = spec
    sup = Supervisor(cfg)
    sup.add("dead", spec)
    wd.watch_once(sup)
    h = sup.healths["dead"]
    assert h.last_check > 0
    assert h.state == "down"
    assert "connection closed" in (h.error or ""), \
        "причина пробы обязана быть в error — иначе UI нечем показать"


# ---------- регрессия P0: gateway не 500 на новом сервере ----------

def test_gateway_channel_down_returns_504_not_keyerror():
    """Новый сервер + недоступный апстрим = 504, а не 500 (audit P0)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from aomg.gateway import create_app

    cfg = Config(gateway_port=9397, groups={"direct": Group(name="Дом")})
    sup = Supervisor(cfg)
    app = create_app(cfg, sup)
    c = TestClient(app, raise_server_exceptions=False)

    spec = _http("newsrv")
    cfg.servers["newsrv"] = spec
    sup.add("newsrv", spec)          # Health создан — как это делает admin

    r = c.post("/newsrv/mcp", json={"jsonrpc": "2.0", "id": 1,
                                    "method": "initialize"})
    assert r.status_code in (502, 504), \
        f"ожидали 502/504, получили {r.status_code}: {r.text[:200]}"
    assert sup.healths["newsrv"].state == "channel_down"


def test_gateway_unknown_server_is_404():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from aomg.gateway import create_app
    cfg = Config(gateway_port=9396, groups={"direct": Group(name="Дом")})
    app = create_app(cfg, Supervisor(cfg))
    c = TestClient(app, raise_server_exceptions=False)
    assert c.post("/nosuch/mcp", json={}).status_code == 404


def test_health_endpoint_serves_snapshot():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from aomg.gateway import create_app
    cfg = Config(gateway_port=9395, groups={"direct": Group(name="Дом")})
    sup = Supervisor(cfg)
    spec = _http("a")
    cfg.servers["a"] = spec
    sup.add("a", spec)
    sup.healths["a"].record("ok", tools=2, ts=time.time())
    c = TestClient(create_app(cfg, sup))
    data = c.get("/health").json()
    assert data["aggregate"] == "ok"
    assert [s["name"] for s in data["servers"]] == ["a"]


def test_health_endpoint_tolerates_removal_during_request():
    """Гонка панель-vs-/health больше не роняет ответ (ADR-0003 I3)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from aomg.gateway import create_app
    cfg = Config(gateway_port=9394, groups={"direct": Group(name="Дом")})
    sup = Supervisor(cfg)
    for n in ("a", "b"):
        s = _http(n)
        cfg.servers[n] = s
        sup.add(n, s)
    c = TestClient(create_app(cfg, sup), raise_server_exceptions=False)
    for _ in range(20):
        sup.remove("a")
        s = _http("a")
        cfg.servers["a"] = s
        sup.add("a", s)
        assert c.get("/health").status_code == 200
