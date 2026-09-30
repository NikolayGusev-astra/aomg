"""Тесты JsonSource (ADR-0002 Phase 1.2): парсинг обеих форм, TTL-кэш,
unreachable -> stale."""
import json
import time

import httpx
import pytest

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from aomg.config import CatalogSourceSpec
from aomg.json_source import JsonSource

LIST_A = [
    {"name": "blender-mcp", "title": "Blender MCP",
     "description": "Blender integration", "url": "https://x.example/mcp"},
    {"name": "unreal-mcp", "command": "npx -y unreal-mcp@2"},
]


def _spec(name="corp"):
    return CatalogSourceSpec(name=name, type="json",
                             url="https://corp.example/mcp-servers")


def _transport(payload_or_exc, counter=None):
    """MockTransport: payload — dict или исключение."""
    def handler(request):
        if counter is not None:
            counter.append(request.url.path)
        if isinstance(payload_or_exc, Exception):
            raise payload_or_exc
        return httpx.Response(200, json=payload_or_exc)
    return httpx.MockTransport(handler)


def _src(tmp_path, transport):
    src = JsonSource(_spec(), cache_dir=tmp_path)
    src._transport = transport
    return src


def test_parses_list_form(tmp_path):
    src = _src(tmp_path, _transport(LIST_A))
    res = src.fetch()
    assert res.state == "ok"
    assert len(res.items) == 2
    blender = res.items[0]
    assert blender["name"] == "blender-mcp"
    assert blender["kind"] == "http"
    assert blender["url"] == "https://x.example/mcp"


def test_parses_wrapped_form(tmp_path):
    src = _src(tmp_path, _transport({"servers": LIST_A}))
    res = src.fetch()
    assert res.state == "ok"
    assert [i["name"] for i in res.items] == ["blender-mcp", "unreal-mcp"]


def test_command_split_into_command_args(tmp_path):
    src = _src(tmp_path, _transport(LIST_A))
    unreal = src.fetch().items[1]
    assert unreal["kind"] == "stdio"
    assert unreal["command"] == "npx"
    assert unreal["args"] == ["-y", "unreal-mcp@2"]


def test_bad_shape_marks_error(tmp_path):
    src = _src(tmp_path, _transport({"hello": "world"}))
    res = src.fetch()
    assert res.state == "error"
    assert res.items == []
    assert "shape" in res.error


def test_records_without_name_or_url_skipped(tmp_path):
    src = _src(tmp_path, _transport([
        {"title": "no name", "url": "https://x"},
        {"name": "empty"},
        {"name": "ok", "url": "https://x/mcp"},
    ]))
    res = src.fetch()
    assert [i["name"] for i in res.items] == ["ok"]


def test_dedup_by_name(tmp_path):
    src = _src(tmp_path, _transport(LIST_A + LIST_A))
    assert len(src.fetch().items) == 2


def test_ttl_cache_no_network_when_fresh(tmp_path):
    counter: list = []
    src = _src(tmp_path, _transport(LIST_A, counter))
    src.fetch()
    assert counter, "первый fetch должен сходить в сеть"
    src.fetch(force=True)   # force=True принудительно, но fetch() умеет TTL
    assert len(counter) == 2, "force идет в сеть"
    items = src.fetch().items
    assert len(items) == 2
    assert len(counter) == 2, "свежий кэш — сеть не трогаем"


def test_unreachable_serves_stale_and_marks(tmp_path):
    src = _src(tmp_path, _transport(LIST_A))
    src.fetch()
    # транспорт сломался
    src._transport = _transport(httpx.ConnectError("net down"))
    res = src.fetch(force=True)
    assert res.state == "unreachable"
    assert len(res.items) == 2, "карточки из дискового кэша"
    assert src.state == "unreachable"


def test_unreachable_no_cache_marks_error(tmp_path):
    src = _src(tmp_path, _transport(httpx.ConnectError("net down")))
    res = src.fetch()
    assert res.state == "error"
    assert res.items == []


def test_ttl_expiry_refetches(tmp_path):
    counter: list = []
    src = _src(tmp_path, _transport(LIST_A, counter))
    src.fetch()
    # протухаем вручную
    cached = json.loads((tmp_path / "catalog-cache-corp.json").read_text(
        encoding="utf-8"))
    cached["ts"] = time.time() - 25 * 3600
    (tmp_path / "catalog-cache-corp.json").write_text(
        json.dumps(cached), encoding="utf-8")
    src.fetch(force=False)
    assert len(counter) == 2, "протухший кэш -> рефетч"
