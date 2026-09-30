"""Тесты секции catalog_sources в конфиге (ADR-0002)."""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from aomg.config import load_config


def _write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_config_parses_catalog_sources(tmp_path):
    p = _write(tmp_path,
               "gateway_port: 9300\n"
               "groups:\n  vpn: {name: VPN, proxy: 'socks5://127.0.0.1:1'}\n"
               "servers: {}\n"
               "catalog_sources:\n"
               "  corp:\n    type: json\n"
               "    url: https://corp-catalog.example/api/v1/mcp-servers\n"
               "    group: vpn\n"
               "    headers: {Authorization: 'Bearer ${LOD_TOKEN}'}\n"
               "  neuraldeep: {hidden: false}\n")
    cfg = load_config(p)
    src = cfg.catalog_sources["corp"]
    assert src.type == "json"
    assert src.group == "vpn"
    assert src.url.endswith("mcp-servers")
    assert src.hidden is False
    # секрет подставляется из env (зададим явно)
    assert cfg.catalog_sources["neuraldeep"].hidden is False


def test_catalog_sources_section_optional(tmp_path):
    p = _write(tmp_path, "gateway_port: 9300\nservers: {}\n")
    cfg = load_config(p)
    assert cfg.catalog_sources == {}


def test_unknown_name_without_type_skipped(tmp_path):
    p = _write(tmp_path,
               "servers: {}\n"
               "catalog_sources:\n"
               "  weird: {url: https://x.example}\n")
    cfg = load_config(p)
    assert "weird" not in cfg.catalog_sources


def test_headers_env_expand(tmp_path, monkeypatch):
    monkeypatch.setenv("LOD_TOKEN", "secret123")
    p = _write(tmp_path,
               "servers: {}\n"
               "catalog_sources:\n"
               "  corp:\n    type: json\n"
               "    url: https://x.example/api\n"
               "    headers: {Authorization: 'Bearer ${LOD_TOKEN}'}\n")
    cfg = load_config(p)
    assert cfg.catalog_sources["corp"].headers["Authorization"] == \
        "Bearer secret123"
