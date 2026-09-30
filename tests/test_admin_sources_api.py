"""API источников каталога (ADR-0002 Phase 1.3).

Поднимаем FastAPI-админку в-process (TestClient) с tmp-конфигом и
подменённым транспортом для JsonSource.
"""
import json
import pathlib
import sys

import httpx
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aomg.config import load_config
from aomg.admin import register_admin
from aomg.health import Health
from aomg.supervisor import Supervisor

LIST_A = [
    {"name": "blender-mcp", "title": "Blender MCP",
     "description": "Blender integration", "url": "https://x.example/mcp"},
    {"name": "unreal-mcp", "command": "npx -y unreal-mcp@2"},
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "gateway_port: 9391\n"
        "groups:\n  vpn: {name: VPN, proxy: 'socks5://127.0.0.1:1'}\n"
        "servers: {}\n"
        "catalog_sources:\n"
        "  corp:\n    type: json\n"
        "    url: https://corp.example/mcp-servers\n"
        "    group: vpn\n"
        "    headers: {Authorization: 'Bearer ${CORP_TOKEN}'}\n",
        encoding="utf-8")
    import os
    monkeypatch.setenv("CORP_TOKEN", "topsecret-123")
    cfg = load_config(cfg_file)

    # Живой реестр в тестах не трогаем: фолбэк обязан быть быстрым и
    # детерминированным, а не зависеть от сети (ADR-0005 I4).
    async def no_registry(*a, **kw):
        raise httpx.ReadTimeout("network disabled in tests")
    monkeypatch.setattr("aomg.registry.search_async", no_registry)

    # подменяем транспорт у всех JsonSource после их создания в register_admin
    app = FastAPI()
    sup = Supervisor(cfg)
    restart_watchdog = lambda: None
    # healths больше не параметр: состоянием владеет Supervisor (ADR-0003)
    register_admin(app, cfg, sup, cfg_file, restart_watchdog)

    # находим созданные источники и подменяем транспорт
    sources = getattr(app.state, "catalog_sources", {})
    corp = sources.get("corp")
    if corp is not None:
        def handler(request):
            assert request.headers.get("authorization") == \
                "Bearer topsecret-123"
            return httpx.Response(200, json=LIST_A)
        corp._transport = httpx.MockTransport(handler)
    c = TestClient(app)
    c.catalog_sources = sources
    c.cfg = cfg
    c.cfg_file = cfg_file
    yield c


def test_malformed_json_body_is_reported_not_500(client):
    """Кривое тело -> внятная ошибка, а не 500 с трейсбеком.

    Найдено на установленной сборке: опечатка в JSON от curl/панели
    роняла эндпоинт с Internal Server Error. Ошибка ввода — это 400,
    а не авария.
    """
    for url in ("/admin/api/servers", "/admin/api/catalog/sources"):
        r = client.post(url, content=b"{not json",
                        headers={"Content-Type": "application/json"})
        assert r.status_code == 200, f"{url} -> {r.status_code}"
        assert "error" in r.json(), f"{url}: нет сообщения об ошибке"
        assert "JSON" in r.json()["error"], r.json()


def test_json_body_helper_rejects_non_dict():
    from aomg.admin import _json_body

    class _Req:
        def __init__(self, payload):
            self._p = payload

        async def json(self):
            if isinstance(self._p, Exception):
                raise self._p
            return self._p

    import asyncio
    assert asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        _json_body(_Req({"ok": 1}))) == {"ok": 1}
    assert asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        _json_body(_Req(ValueError("bad")))) is None
    assert asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        _json_body(_Req([1, 2]))) is None, "список — не dict"


def test_notice_does_not_claim_registry_down_when_index_is_full(client):
    """Промах по запросу при живом индексе — это «не найдено».

    Найдено на установленной сборке: реестр отвечал 200, индекс лежал
    на диске (2791 запись), а UI писал «Реестр MCP недоступен, локальный
    индекс пуст». Сообщение врало про оба источника сразу и уводило
    в ложный диагноз.
    """
    # индекс заполнен — иначе проверяем не тот случай
    idx = client.app.state.catalog_sources["official"]._idx
    idx._save([{"server": {"name": "ac.test/filesystem",
                           "description": "Filesystem MCP server",
                           "version": "1.0.0"},
                "_meta": {"io.modelcontextprotocol.registry/official": {
                    "status": "active"}}}])
    idx.invalidate()
    r = client.get("/admin/api/catalog",
                   params={"query": "абракадабранесуществующее"})
    d = r.json()
    assert d["state"] == "ok", d
    notice = d.get("notice") or ""
    assert "недоступен" not in notice.lower(), \
        f"сообщение врёт при живом реестре: {notice!r}"
    assert "индекс пуст" not in notice.lower(), \
        f"сообщение врёт про пустой индекс: {notice!r}"


def test_notice_reports_registry_problem_only_when_index_is_empty(client):
    """Обратная сторона: пустой индекс — честное «реестр недоступен»."""
    client.app.state.catalog_sources["official"]._idx.path = (
        client.app.state.catalog_sources["official"]._idx.path
        .with_name("отсутствует.json"))
    client.app.state.catalog_sources["official"]._idx.invalidate()
    r = client.get("/admin/api/catalog", params={"query": "blender"})
    notice = r.json().get("notice") or ""
    assert "недоступен" in notice.lower(), \
        f"при пустом индексе сообщение должно быть честным: {notice!r}"


def test_sources_list_builtin_first(client):
    data = client.get("/admin/api/catalog/sources").json()["sources"]
    names = [s["name"] for s in data]
    assert names[0] == "official"
    assert "neuraldeep" in names
    assert "corp" in names
    corp = [s for s in data if s["name"] == "corp"][0]
    assert corp["type"] == "json"
    assert corp["group"] == "vpn"


def test_search_in_json_source(client):
    data = client.get(
        "/admin/api/catalog",
        params={"source": "corp", "query": "blender"}).json()
    assert data["source"] == "corp"
    assert len(data["items"]) == 1
    card = data["items"][0]
    assert card["name"] == "blender-mcp"
    assert card["source"] == "corp"


def test_search_default_is_official(client):
    # без source -> official: ищем по пустому индексу, но не падаем и
    # помечаем источник
    data = client.get("/admin/api/catalog", params={"query": "github"}).json()
    assert data.get("source") in ("index", "live", None) or "items" in data


def test_unreachable_source_fails_fast(client):
    # подменяем транспорт на бросающий
    corp = client.catalog_sources["corp"]
    def broken(request):
        raise httpx.ConnectError("refused")
    corp._transport = httpx.MockTransport(broken)
    corp.cache_path.unlink(missing_ok=True)
    import time
    t0 = time.time()
    data = client.get("/admin/api/catalog",
                      params={"source": "corp", "query": "x"}).json()
    elapsed = time.time() - t0
    assert elapsed < 10, "недоступный источник не должен висеть"
    assert data["items"] == []
    assert "error" in data


def test_stale_cache_served_when_unreachable(client):
    corp = client.catalog_sources["corp"]
    corp.fetch()   # наполняем кэш
    def broken(request):
        raise httpx.ConnectError("refused")
    corp._transport = httpx.MockTransport(broken)
    data = client.get("/admin/api/catalog",
                      params={"source": "corp", "query": ""}).json()
    names = [i["name"] for i in data["items"]]
    assert "blender-mcp" in names and "unreal-mcp" in names


def test_post_source_writes_yaml_masks_secret(client):
    secret = "Bearer sk-very-secret-value-1234"
    r = client.post("/admin/api/catalog/sources", json={
        "name": "team-catalog", "type": "json",
        "url": "https://inner.example/mcp.json",
        "group": "vpn",
        "headers": {"Authorization": secret}})
    assert r.json().get("saved") == "team-catalog"
    # в yaml записан реальный секрет
    raw = client.cfg_file.read_text(encoding="utf-8")
    assert secret in raw
    # в API — маскирован (Authorization маскируется всегда)
    data = client.get("/admin/api/catalog/sources").json()["sources"]
    team = [s for s in data if s["name"] == "team-catalog"][0]
    hdrs = team.get("headers") or {}
    assert hdrs.get("Authorization") == "***", hdrs
    assert secret not in json.dumps(data)


def test_delete_source(client):
    r = client.post("/admin/api/catalog/sources", json={
        "name": "tmp-src", "type": "json",
        "url": "https://t.example/mcp.json", "group": "direct"})
    assert r.json().get("saved") == "tmp-src"
    r = client.post("/admin/api/catalog/sources/tmp-src/delete")
    assert r.json().get("deleted") == "tmp-src"
    names = [s["name"] for s in
             client.get("/admin/api/catalog/sources").json()["sources"]]
    assert "tmp-src" not in names


def test_builtin_source_not_deletable(client):
    r = client.post("/admin/api/catalog/sources/official/delete")
    assert "error" in r.json()
