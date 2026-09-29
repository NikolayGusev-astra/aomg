"""Клиент официального реестра registry.modelcontextprotocol.io.

Вход: /v0/servers?search=&limit=&cursor=. Выход: RegistryEntry с формой
полей для API-ключей (из remotes[].headers, isSecret) или пакетом для
локальной установки (packages[], с pin версии).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx

REGISTRY_BASE = "https://registry.modelcontextprotocol.io"


@dataclass
class RegistryEntry:
    name: str
    title: str
    description: str
    version: str
    kind: str                                  # "http" | "stdio"
    remote_url: str | None = None              # для kind=http
    headers: list[dict] = field(default_factory=list)
    package: dict | None = None                # {"manager","identifier"} для stdio
    pinned_version: str | None = None
    form_fields: list[dict] = field(default_factory=list)  # для UI формы ключей


def parse_server_entry(row: dict) -> RegistryEntry:
    srv = row.get("server") or {}
    meta = ((row.get("_meta") or {})
            .get("io.modelcontextprotocol.registry/official") or {})
    if meta.get("status") != "active":
        raise ValueError(f"inactive server: {srv.get('name')} "
                         f"(status={meta.get('status')})")

    entry = RegistryEntry(
        name=srv.get("name", ""), title=srv.get("title") or srv.get("name", ""),
        description=srv.get("description", ""),
        version=srv.get("version", ""), kind="http")

    remotes = srv.get("remotes") or []
    packages = srv.get("packages") or []
    if remotes:
        remote = remotes[0]
        entry.remote_url = remote.get("url")
        for h in remote.get("headers") or []:
            entry.headers.append(h)
            entry.form_fields.append({
                "name": h.get("name", "Authorization"),
                "secret": bool(h.get("isSecret")),
                "required": bool(h.get("isRequired")),
                "description": h.get("description", ""),
                "template": h.get("value", ""),
            })
    elif packages:
        pkg = packages[0]
        entry.kind = "stdio"
        entry.package = {"manager": pkg.get("registry_type", "npm"),
                         "identifier": pkg.get("identifier", "")}
        entry.pinned_version = pkg.get("version")
        # env-переменные пакета: форма ключей для stdio тоже из данных
        for ev in (pkg.get("environmentVariables")
                   or pkg.get("environment_variables") or []):
            entry.form_fields.append({
                "name": ev.get("name", "API_KEY"),
                "secret": bool(ev.get("isSecret")),
                "required": bool(ev.get("isRequired")),
                "description": ev.get("description", ""),
                "template": ev.get("value", ""),
            })
    else:
        raise ValueError(f"no remotes/packages for {entry.name}")
    return entry


def search(query: str, limit: int = 20,
           timeout: float = 20.0) -> list[RegistryEntry]:
    params = {"search": query, "limit": str(limit)}
    out: list[RegistryEntry] = []
    with httpx.Client(timeout=timeout) as c:
        r = c.get(f"{REGISTRY_BASE}/v0/servers", params=params)
        r.raise_for_status()
        for row in r.json().get("servers", []):
            try:
                out.append(parse_server_entry(row))
            except ValueError:
                continue  # неактивные и пустые пропускаем молча
    return out
