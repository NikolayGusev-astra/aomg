"""Тесты каталога: официальный реестр + кастомные источники (neuraldeep)."""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from aomg.catalog import (CatalogItem, NeuralDeepSource, OfficialSource,
                          SOURCES)


def test_official_source_returns_mcp_items():
    src = OfficialSource()
    items = src.list_items("github", limit=5)
    assert items, "официальный реестр должен отдавать записи"
    assert all(i.source == "official" and i.kind == "mcp" for i in items)


RSC_PAGE = rb'''
<script>self.__next_f.push([1,"...\"href\":\"/mcp/rusender-mcp\"..."])</script>
<script>self.__next_f.push([1,"...\"href\":\"/mcp/spring-ssh-mcp\"..."])</script>
<script>self.__next_f.push([1,"...\"href\":\"/skills/spb-gorzdrav-skill\"..."])</script>
<script>self.__next_f.push([1,"...\"href\":\"/skills/validator\"..."])</script>
'''


def test_neuraldeep_parses_slugs_and_skips_validator(monkeypatch):
    src = NeuralDeepSource()

    class FakeResp:
        content = RSC_PAGE
        def raise_for_status(self): pass

    monkeypatch.setattr(src, "_fetch", lambda path: RSC_PAGE)
    items = src.list_items()
    names = {(i.kind, i.name) for i in items}
    assert ("mcp", "rusender-mcp") in names
    assert ("skills", "spb-gorzdrav-skill") in names
    assert all(n != "validator" for _, n in names), "validator служебный"
    assert len(names) == 3


def test_neuraldeep_get_item_extracts_install_and_repo(monkeypatch):
    src = NeuralDeepSource()
    page = b'''<html><h1>rusender-mcp</h1>
MCP-servers RuSender email automation.
npx skillsbd add Rusender/rusender-mcp/rusender-mcp
github.com/Rusender/rusender-mcp
</html>'''
    monkeypatch.setattr(src, "_fetch", lambda path: page)
    item = src.get_item("rusender-mcp")
    assert item is not None
    assert "npx skillsbd add" in item.install_hint
    assert item.repo_url == "https://github.com/Rusender/rusender-mcp"
    assert item.ru_service is True


def test_sources_registry_has_official_and_neuraldeep():
    assert "official" in SOURCES and "neuraldeep" in SOURCES
