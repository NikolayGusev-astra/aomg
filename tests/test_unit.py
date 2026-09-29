"""Unit-тесты AOMG: config, health-логика, client config sync, registry-парсинг.

Запуск: python -m pytest tests/ -v
"""
import json
import pathlib
import sys

import pytest
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from aomg.config import load_config, atomic_save_config
from aomg.health import Health, aggregate_state, WORST_ORDER
from aomg.client_sync import sync_client_config
from aomg.registry import parse_server_entry, RegistryEntry


# ---------- config ----------

def test_load_config_derives_kind_from_url_or_command(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.safe_dump({
        "gateway_port": 9300,
        "groups": {"direct": {"name": "Дом", "proxy": None}},
        "servers": {
            "jira": {"command": "corp-mcp.exe", "group": "direct"},
            "weather": {"url": "https://api.example.com/mcp", "group": "direct"},
        },
    }), encoding="utf-8")
    cfg = load_config(cfg_file)
    assert cfg.servers["jira"].kind == "stdio"
    assert cfg.servers["weather"].kind == "http"


def test_load_config_rejects_server_with_neither_url_nor_command(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.safe_dump({
        "groups": {"direct": {"name": "Дом", "proxy": None}},
        "servers": {"broken": {"group": "direct"}},
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="url|command"):
        load_config(cfg_file)


def test_atomic_save_roundtrip(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("gateway_port: 9300\n", encoding="utf-8")
    atomic_save_config(cfg_file, {"gateway_port": 9400, "new": {"a": 1}})
    loaded = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
    assert loaded["gateway_port"] == 9400 and loaded["new"] == {"a": 1}


# ---------- health ----------

def test_aggregate_worst_state_wins():
    states = [Health(state="ok", tools=5), Health(state="channel_down", tools=0)]
    assert aggregate_state(states) == "channel_down"
    states.append(Health(state="down", tools=0))
    assert aggregate_state(states) == "down"


def test_worst_order_covers_all_states():
    assert set(WORST_ORDER) == {"ok", "reconnecting", "channel_down", "down"}


# ---------- client config sync ----------

def test_sync_writes_url_entries_and_is_idempotent(tmp_path):
    client_cfg = tmp_path / "client-config.yaml"
    client_cfg.write_text(yaml.safe_dump({
        "mcp_servers": {"old_manual": {"command": "uvx", "args": ["x"]}},
    }), encoding="utf-8")
    gw = {"jira": "http://127.0.0.1:9300/jira/mcp",
          "weather": "http://127.0.0.1:9300/weather/mcp"}
    sync_client_config(client_cfg, gw)
    sync_client_config(client_cfg, gw)  # idempotent
    data = yaml.safe_load(client_cfg.read_text(encoding="utf-8"))
    assert data["mcp_servers"]["jira"]["url"] == gw["jira"]
    assert data["mcp_servers"]["jira"]["x-aomg"] is True  # маркер-защита
    assert data["mcp_servers"]["weather"]["url"] == gw["weather"]
    assert "old_manual" in data["mcp_servers"], "чужие записи не трогаем"


def test_sync_overwrites_stale_aomg_entry(tmp_path):
    client_cfg = tmp_path / "client-config.yaml"
    client_cfg.write_text(yaml.safe_dump({
        "mcp_servers": {"jira": {"url": "http://127.0.0.1:OLD/jira/mcp"}},
    }), encoding="utf-8")
    sync_client_config(client_cfg, {"jira": "http://127.0.0.1:9300/jira/mcp"})
    data = yaml.safe_load(client_cfg.read_text(encoding="utf-8"))
    assert data["mcp_servers"]["jira"]["url"] == "http://127.0.0.1:9300/jira/mcp"


# ---------- registry ----------

REGISTRY_ROW_HTTP = {
    "server": {
        "name": "ai.smithery/test-server",
        "title": "Test Server",
        "description": "Does things.",
        "version": "1.2.3",
        "remotes": [{
            "type": "streamable-http",
            "url": "https://remote.example/mcp",
            "headers": [{"name": "Authorization",
                         "value": "Bearer {api_key}",
                         "isSecret": True,
                         "isRequired": True,
                         "description": "Bearer token"}],
        }],
    },
    "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active"}},
}

REGISTRY_ROW_STDIO = {
    "server": {
        "name": "io.github/x/time-mcp",
        "title": "Time",
        "description": "Time tools.",
        "version": "0.4.0",
        "packages": [{"registry_type": "npm", "identifier": "mcp-server-time",
                      "version": "0.4.0", "transport": {"type": "stdio"}}],
    },
    "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active"}},
}


def test_parse_http_entry_yields_secret_form_and_url():
    e = parse_server_entry(REGISTRY_ROW_HTTP)
    assert e.kind == "http"
    assert e.remote_url == "https://remote.example/mcp"
    assert e.form_fields == [{"name": "Authorization", "secret": True,
                              "required": True, "description": "Bearer token",
                              "template": "Bearer {api_key}"}]


def test_parse_stdio_entry_yields_package_pin():
    e = parse_server_entry(REGISTRY_ROW_STDIO)
    assert e.kind == "stdio"
    assert e.package == {"manager": "npm", "identifier": "mcp-server-time"}
    assert e.pinned_version == "0.4.0"


def test_parse_skips_inactive_entries():
    row = json.loads(json.dumps(REGISTRY_ROW_HTTP))
    row["_meta"]["io.modelcontextprotocol.registry/official"]["status"] = "deleted"
    with pytest.raises(ValueError, match="inactive"):
        parse_server_entry(row)
