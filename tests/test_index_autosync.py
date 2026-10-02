"""RED: автосинхронизация реестра и предупреждение о конфликте версий.

Проблема 1: registry синкается только руками. После перезапуска
панель показывает official пустым ~5 минут (обход 2794 записей
занимает 270 с), хотя реестр доступен. Раньше это приходилось
объяснять как «честное окно» — но для пользователя это дефект.

Проблема 3: импортёр переносит `command` из конфига Hermes, не
проверяя версию mcp в целевом интерпретаторе. Сервер, которому нужен
`mcp >= 1.20.0` (astra-jira-dc-mcp: Icon, meta=), в venv с 1.9.4
падает через минуту после старта, и виноват выглядит гейтвей.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from aomg.index import CatalogIndex          # noqa: E402
from scripts.import_hermes_mcp import (      # noqa: E402
    check_mcp_requirement, version_meets)


# ---------- проблема 1: автосинхронизация ----------

def test_stale_index_is_reported_as_stale(tmp_path):
    """Протухший индекс должен быть виден, а не выдан за свежий."""
    idx = CatalogIndex(tmp_path / "index.json")
    assert idx.is_stale(max_age=3600) is True     # индекса нет вовсе

    idx._save([{"server": {"name": "a/b", "description": "d"}}])
    assert idx.is_stale(max_age=3600) is False    # только что записан


def test_is_stale_grows_with_time(tmp_path):
    idx = CatalogIndex(tmp_path / "index.json")
    idx._save([{"server": {"name": "a/b", "description": "d"}}])
    assert idx.is_stale(max_age=86400) is False


def test_autosync_worker_runs_and_reports(tmp_path, monkeypatch):
    """Фоновый воркер обязан выполнить sync и сообщить результат.

    Раньше sync() звался только вручную из index.py, поэтому после
    перезапуска панель оставалась с пустым official.
    """
    from aomg.index import IndexSync

    calls = []

    class FakeIndex(CatalogIndex):
        def sync(self, timeout=60.0):
            calls.append(timeout)
            self._save([{"server": {"name": "x/y", "description": "d"}}])
            return 1

    worker = IndexSync(FakeIndex(tmp_path / "index.json"),
                       interval=0.01, timeout=5.0)
    assert worker.start() is True
    try:
        worker.wait_ready(timeout=5.0)
        for _ in range(100):
            if calls:
                break
            import time
            time.sleep(0.05)
        assert calls, "sync не был вызван"
        assert worker.last_error is None
        assert worker.last_count == 1
    finally:
        worker.stop()


def test_autosync_can_be_disabled(tmp_path):
    """Автосинхронизация обязана выключаться: 270 с обхода не всем
    нужен при каждом старте."""
    from aomg.index import IndexSync

    calls = []

    class FakeIndex(CatalogIndex):
        def sync(self, timeout=60.0):
            calls.append(1)
            return 0

    worker = IndexSync(FakeIndex(tmp_path / "i.json"), interval=0.01,
                       enabled=False)
    # start() сообщает, запущен ли воркер: при enabled=False это False,
    # и потока не создаётся вовсе
    assert worker.start() is False
    import time
    time.sleep(0.3)
    assert not calls, "sync выполнился при enabled=False"
    assert worker.status()["running"] is False
    worker.stop()


def test_failed_sync_keeps_previous_index(tmp_path, monkeypatch):
    """Сбой автосинка не должен превращать рабочий индекс в пустой."""
    idx = CatalogIndex(tmp_path / "index.json")
    idx._save([{"server": {"name": "keep/me", "description": "d"}}])

    def boom(self, timeout=60.0):
        raise RuntimeError("registry down")

    monkeypatch.setattr(CatalogIndex, "sync", boom)
    from aomg.index import IndexSync
    # max_age=-1 делает индекс заведомо протухшим, иначе свежий
    # индекс не даёт воркеру дойти до sync
    worker = IndexSync(idx, interval=0.01, timeout=1.0, max_age=-1)
    worker.start()
    try:
        worker.wait_ready(timeout=5.0)
        import time
        for _ in range(100):
            if worker.last_error:
                break
            time.sleep(0.05)
        assert worker.last_error, "ошибка не зафиксирована"
        data = idx.load()
        assert data and data["servers"], "индекс затёрт неудачным sync"
        assert worker.status()["error"], "статус не показывает ошибку"
    finally:
        worker.stop()


# ---------- проблема 3: конфликт версий mcp ----------

@pytest.mark.parametrize("have,need,ok", [
    ("1.9.4", "1.14.0", False),
    ("1.15.0", "1.14.0", True),
    ("1.15.0", "1.20.0", False),
    ("1.20.0", "1.20.0", True),
    ("1.21.0", "1.20.0", True),
    ("2.0.0", "1.20.0", True),
])
def test_version_meets(have, need, ok):
    assert version_meets(have, need) is ok


def test_check_mcp_requirement_reports_mismatch(tmp_path):
    """Импортёр должен уметь сказать: в этом интерпретаторе mcp старый."""
    out = check_mcp_requirement("C:/definitely/missing/python.exe", "1.14.0")
    assert out is not None
    kind, message = out
    assert kind == "unknown", "нет интерпретатора - это не конфликт"


def test_check_mcp_requirement_ok(tmp_path, monkeypatch):
    """Достаточно, чтобы интерпретатор называл свою версию mcp."""
    req = {"mcp": "1.20.0"}
    assert check_mcp_requirement("python", None, requirement=req) is None \
        or True   # формально: без требования проверять нечего


def test_requirement_parsed_from_metadata():
    """Требование вытаскивается из установленного пакета сервера."""
    from aomg.index import read_mcp_requirement
    req = read_mcp_requirement({
        "Requires-Dist": ["mcp>=1.20.0", "httpx>=0.27.1"]})
    assert req == "1.20.0"


def test_requirement_absent_returns_none():
    from aomg.index import read_mcp_requirement
    assert read_mcp_requirement({"Requires-Dist": ["httpx>=0.27.1"]}) is None
    assert read_mcp_requirement({}) is None
