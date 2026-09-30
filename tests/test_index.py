"""Тесты локального индекса каталога (aomg/index.py).

Сеть не трогаем: httpx-клиент подменяется через monkeypatch на фейковый
transport, диск — через tmp_path.
"""
import json
import time

import httpx
import pytest

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from aomg.index import CatalogIndex


def _row(name="io.github.x/mcp-server", title="MCP Server",
         desc="A test server", status="active"):
    return {"server": {
                "name": name, "title": title, "description": desc,
                "version": "1.0.0",
                "remotes": [{"url": "https://example.com/mcp",
                             "headers": [{"name": "X-Api-Key",
                                          "value": "{auth}",
                                          "isSecret": True,
                                          "isRequired": True}]}]},
            "_meta": {"io.modelcontextprotocol.registry/official":
                      {"status": status}}}


class _FakeTransport(httpx.BaseTransport):
    """Отдаёт заготовленные страницы /v0/servers по очереди."""

    def __init__(self, pages):
        self.pages = list(pages)  # payload-словари

    def handle_request(self, request):
        page = self.pages.pop(0) if self.pages else {"servers": []}
        return httpx.Response(200, json=page)


def _patch_transport(monkeypatch, pages):
    t = _FakeTransport(pages)
    orig_client = httpx.Client  # до подмены: иначе рекурсия в фейке

    def fake_client(*a, **kw):
        kw.pop("transport", None)
        return orig_client(transport=t, **kw)
    monkeypatch.setattr("aomg.index.httpx.Client", fake_client)


def _index_with(tmp_path, data=None):
    idx = CatalogIndex(tmp_path / "registry-index.json")
    if data is not None:
        idx.path.write_text(json.dumps(data), encoding="utf-8")
    return idx


def test_sync_paginates_and_dedups_by_short_name(tmp_path, monkeypatch):
    # один пакет под двумя namespace -> в индекс попадает один
    idx = _index_with(tmp_path)
    _patch_transport(monkeypatch, [
        {"servers": [_row("io.github.a/mcp-server"),
                     _row("io.b/mcp-server")],
         "metadata": {"nextCursor": "p2"}},
        {"servers": [_row("io.github.c/other")], "metadata": {}},
    ])
    n = idx.sync(timeout=5.0)
    assert n == 2  # mcp-server задедуплен до одной записи, other — вторая
    data = idx.load()
    assert data and data["synced_at"] > 0
    shorts = {r["server"]["name"].split("/")[-1] for r in data["servers"]}
    assert shorts == {"mcp-server", "other"}


def test_search_substring_and_empty_query(tmp_path):
    now = time.time()
    data = {"synced_at": now, "servers": [
        _row("io.github.a/github-mcp", "GitHub MCP", "GitHub issues and PRs"),
        _row("io.github.b/jira-mcp", "Jira MCP", "Jira integration"),
        _row("io.github.c/weather", "Weather", "forecast"),
    ]}
    idx = _index_with(tmp_path, data)
    items = idx.search("github")
    assert len(items) == 1
    assert items[0]["name"] == "github-mcp"
    assert items[0]["form_fields"], "поле секрета должно прийти в карточке"
    # поиск по описанию тоже находит
    assert len(idx.search("integration")) == 1
    # пустой запрос -> первые записи без фильтра
    assert len(idx.search("")) == 3


def test_search_skips_inactive(tmp_path):
    data = {"synced_at": time.time(), "servers": [
        _row("io.github.a/ok-server", status="active"),
        _row("io.github.b/dead-server", status="deprecated"),
    ]}
    idx = _index_with(tmp_path, data)
    names = [i["name"] for i in idx.search("")]
    assert names == ["ok-server"]


def test_stale_logic_and_bg_refresh(tmp_path, monkeypatch):
    old = time.time() - 25 * 3600
    data = {"synced_at": old, "servers": [_row("io.github.a/github-mcp")]}
    idx = _index_with(tmp_path, data)
    assert idx.is_stale() is True

    fresh = {"synced_at": time.time(), "servers": [_row("io.github.a/github-mcp")]}
    idx.path.write_text(json.dumps(fresh), encoding="utf-8")
    assert idx.is_stale() is False

    # ensure_fresh на свежем индексе ничего не делает
    assert idx.ensure_fresh() is False

    # на устаревшем — запускает фоновый sync и не блокирует ответ
    old_data = {"synced_at": time.time() - 25 * 3600,
                "servers": [_row("io.github.a/github-mcp")]}
    idx.path.write_text(json.dumps(old_data), encoding="utf-8")

    def fake_sync(timeout=60.0):
        time.sleep(0.2)
        idx._save([_row("io.github.a/github-mcp")])
        return 1
    monkeypatch.setattr(idx, "sync", fake_sync)
    t0 = time.time()
    assert idx.ensure_fresh() is True
    assert time.time() - t0 < 0.1, "ensure_fresh не должен ждать sync"
    # ждём завершения фона
    deadline = time.time() + 3
    while idx.syncing and time.time() < deadline:
        time.sleep(0.02)
    assert idx.is_stale() is False


def test_sync_failure_keeps_old_index(tmp_path, monkeypatch):
    old = {"synced_at": time.time() - 3600, "servers": [_row()]}
    idx = _index_with(tmp_path, old)

    def boom(*a, **kw):
        raise httpx.ConnectError("network down")
    monkeypatch.setattr("aomg.index.httpx.Client", boom)
    with pytest.raises(httpx.ConnectError):
        idx.sync()
    # старый индекс цел
    assert idx.load()["servers"] == old["servers"]

    # ensure_fresh ошибку глотает — индекс остаётся как был
    monkeypatch.setattr(idx, "is_stale", lambda: True)
    assert idx.ensure_fresh() is True
    deadline = time.time() + 3
    while idx.syncing and time.time() < deadline:
        time.sleep(0.02)
    assert idx.load()["servers"] == old["servers"]
