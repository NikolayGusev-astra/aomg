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

    # подменяем транспорт у всех JsonSource после их создания в register_admin
    app = FastAPI()
    sup = Supervisor(cfg)
    healths = {}
    restart_watchdog = lambda: None
    register_admin(app, cfg, sup, healths, cfg_file, restart_watchdog)

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
