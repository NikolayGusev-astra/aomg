"""Реестры-источники каталога: единый интерфейс RegistrySource.

Источник 1: официальный registry.modelcontextprotocol.io (уже есть, registry.py)
Источник 2: кастомные каталоги, пока neuraldeep.ru (российские сервисы).
NeuralDeep — Next.js SPA без публичного JSON API: данные достаются из RSC-
потока страницы /skills (self.__next_f.push чанки) или со страниц /mcp/<slug>
(там есть команда установки и GitHub-репо).

Адаптер отдаёт те же RegistryEntry, UI разницы не знает.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import httpx

from .registry import RegistryEntry


@dataclass
class CatalogItem:
    """Единая карточка каталога для UI (нейтральна к источнику)."""
    source: str                 # "official" | "neuraldeep" | ...
    name: str
    kind: str                   # "mcp" | "skill" | "cli"
    title: str = ""
    description: str = ""
    entry: RegistryEntry | None = None      # если конвертируется в сервер
    install_hint: str = ""                  # напр. "npx skillsbd add ..."
    repo_url: str = ""
    ru_service: bool = False                # флаг для бейджа «РФ-сервис»


class CatalogSource(ABC):
    name: str = "base"

    @abstractmethod
    def list_items(self) -> list[CatalogItem]: ...

    @abstractmethod
    def get_item(self, slug: str) -> CatalogItem | None: ...


# ---------- официальный реестр ----------

class OfficialSource(CatalogSource):
    name = "official"

    def list_items(self, query: str = "", limit: int = 30):
        from . import registry
        return [CatalogItem(source=self.name, name=e.name, kind="mcp",
                            title=e.title, description=e.description, entry=e)
                for e in registry.search(query, limit)]

    def get_item(self, slug: str):
        items = self.list_items(slug, limit=5)
        return items[0] if items else None


# ---------- NeuralDeep ----------

ND_BASE = "https://neuraldeep.ru"

# В RSC-потоке карточки лежат как escape-JSON. Достаём пары
# (тип, slug) из href-ов /mcp/<slug> и /skills/<slug>.
_HREF = re.compile(rb'\\?"\/(mcp|skills)\\?\/([a-z0-9-]+)\\?"')


class NeuralDeepSource(CatalogSource):
    name = "neuraldeep"

    def __init__(self, base: str = ND_BASE, timeout: float = 20.0):
        self.base = base
        self.timeout = timeout

    def _fetch(self, path: str) -> bytes:
        r = httpx.get(f"{self.base}{path}", timeout=self.timeout,
                      follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0 AOMG/0.1"})
        r.raise_for_status()
        return r.content

    def list_items(self) -> list[CatalogItem]:
        html = self._fetch("/skills")
        seen: set[tuple[str, str]] = set()
        items: list[CatalogItem] = []
        for kind, slug in _HREF.findall(html):
            k = kind.decode()
            s = slug.decode()
            if (k, s) in seen or s in ("validator",):
                continue
            seen.add((k, s))
            items.append(CatalogItem(source=self.name, name=s, kind=k))
        return items

    def get_item(self, slug: str) -> CatalogItem | None:
        kind = "mcp" if "/mcp/" in slug or slug.startswith("mcp-") else "skills"
        path = slug if slug.startswith("/") else f"/{kind}/{slug}"
        try:
            html = self._fetch(path).decode("utf-8", errors="replace")
        except httpx.HTTPStatusError:
            return None
        text = re.sub(r"<[^>]+>", "\n", html)
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        # описание: первый абзац после H1-слага
        title = slug.rsplit("/", 1)[-1]
        desc = ""
        for i, l in enumerate(lines):
            if title in l and i + 1 < len(lines):
                desc = lines[i + 1][:400]
                break
        m = re.search(r"npx skillsbd add [A-Za-z0-9_/.-]+", html)
        repo = ""
        rm = re.search(r'github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)', html)
        if rm:
            repo = f"https://github.com/{rm.group(1)}"
        return CatalogItem(source=self.name, name=title, kind="mcp",
                           title=title, description=desc,
                           install_hint=m.group(0) if m else "",
                           repo_url=repo, ru_service=True)


SOURCES: dict[str, CatalogSource] = {
    "official": OfficialSource(),
    "neuraldeep": NeuralDeepSource(),
}


def catalog_search(query: str, sources: list[str] | None = None,
                   limit: int = 20) -> list[CatalogItem]:
    """Поиск по выбранным источникам (или по всем)."""
    out: list[CatalogItem] = []
    for name in sources or list(SOURCES):
        src = SOURCES.get(name)
        if src is None:
            continue
        try:
            items = src.list_items() if hasattr(src, "list_items") else []
        except Exception:
            continue
        q = query.lower()
        out += [i for i in items
                if q in i.name.lower() or q in i.description.lower()]
    return out[:limit]
