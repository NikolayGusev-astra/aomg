"""Прописывание url-записей AOMG в config.yaml MCP-клиента.

Только записи под своим ключом aomg_gateway — чужие (ручные stdio-записи)
не трогаем. Idempotent.
"""
from __future__ import annotations

from pathlib import Path

import yaml

AOMG_MARKER_KEY = "x-aomg"


def sync_client_config(client_cfg_path: Path, gateway_urls: dict[str, str]) -> None:
    if client_cfg_path.exists():
        data = yaml.safe_load(client_cfg_path.read_text(encoding="utf-8")) or {}
    else:
        data = {}
    servers = data.setdefault("mcp_servers", {})

    for name, url in gateway_urls.items():
        entry = servers.get(name)
        if entry is None:
            servers[name] = {"url": url, AOMG_MARKER_KEY: True}
        elif entry.get(AOMG_MARKER_KEY) or (
                isinstance(entry, dict) and set(entry) <= {"url", AOMG_MARKER_KEY}):
            entry["url"] = url
            entry[AOMG_MARKER_KEY] = True
        # иначе: запись заведена вручную — не трогаем

    tmp = client_cfg_path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                   encoding="utf-8")
    tmp.replace(client_cfg_path)
