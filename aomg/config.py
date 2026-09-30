"""Конфиг AOMG: загрузка, вывод kind, атомарное сохранение."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Group:
    name: str
    proxy: str | None = None
    check: str | None = None


@dataclass
class ServerSpec:
    name: str
    kind: str                 # "stdio" | "http", выводится, не хранится
    group: str = "direct"
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    proxy: str | None = None  # override прокси группы
    pinned_version: str | None = None


@dataclass
class CatalogSourceSpec:
    """Источник каталога (ADR-0002). Встроенные (neuraldeep) — без type/url;
    пользовательские — type=json + url, egress = группа `group`."""
    name: str
    type: str = ""                # "" | "json"
    url: str = ""
    group: str = "direct"
    headers: dict[str, str] = field(default_factory=dict)
    hidden: bool = False


@dataclass
class Config:
    gateway_port: int = 9300
    groups: dict[str, Group] = field(default_factory=dict)
    servers: dict[str, ServerSpec] = field(default_factory=dict)
    catalog_sources: dict[str, CatalogSourceSpec] = field(default_factory=dict)

    def egress_for(self, server: ServerSpec) -> str | None:
        if server.proxy is not None:
            return server.proxy
        g = self.groups.get(server.group)
        return g.proxy if g else None


def load_config(path: Path) -> Config:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = Config(gateway_port=int(raw.get("gateway_port", 9300)))
    for gname, g in (raw.get("groups") or {}).items():
        cfg.groups[gname] = Group(name=g.get("name", gname),
                                  proxy=g.get("proxy"),
                                  check=g.get("check"))
    def _expand(s: str) -> str:
        """${VAR} -> значение. Сначала process-env, затем User-окружение
        Windows (HKCU\\Environment): многие секреты живут только там,
        а os.path.expandvars их не видит и оставляет ${VAR} литералом."""
        out = os.path.expandvars(s)
        if out == s and s.startswith("${") and s.endswith("}"):
            import winreg
            name = s[2:-1]
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                    val, _ = winreg.QueryValueEx(k, name)
                    return str(val)
            except OSError:
                pass
        return out

    if "direct" not in cfg.groups:
        cfg.groups["direct"] = Group(name="Дом (напрямую)", proxy=None)
    for sname, s in (raw.get("servers") or {}).items():
        has_url, has_cmd = bool(s.get("url")), bool(s.get("command"))
        if has_url == has_cmd:
            raise ValueError(
                f"server '{sname}': нужен ровно один из url/command")
        env = {_k: _expand(v) if isinstance(v, str) else v
               for _k, v in (s.get("env") or {}).items()}
        headers = {_k: _expand(v) if isinstance(v, str) else v
                   for _k, v in (s.get("headers") or {}).items()}
        cfg.servers[sname] = ServerSpec(
            name=sname,
            kind="http" if has_url else "stdio",
            group=s.get("group", "direct"),
            command=s.get("command"), args=s.get("args") or [],
            env=env, url=s.get("url"),
            headers=headers, proxy=s.get("proxy"),
            pinned_version=s.get("pinned_version"))
    for cname, s in (raw.get("catalog_sources") or {}).items():
        if not isinstance(s, dict):
            continue
        spec = CatalogSourceSpec(
            name=cname, type=s.get("type", ""),
            url=s.get("url", ""), group=s.get("group", "direct"),
            headers={k: _expand(v) if isinstance(v, str) else v
                     for k, v in (s.get("headers") or {}).items()},
            hidden=bool(s.get("hidden", False)))
        # неизвестное имя без type — warning и пропуск (не роняем конфиг)
        if not spec.type and cname != "neuraldeep":
            print(f"warning: catalog source '{cname}' has no type, skipped")
            continue
        cfg.catalog_sources[cname] = spec
    return cfg


def atomic_save_config(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                   encoding="utf-8")
    os.replace(tmp, path)
