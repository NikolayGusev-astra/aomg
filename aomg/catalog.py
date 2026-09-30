"""Встроенный html-адаптер каталога: neuraldeep.ru (ADR-0005).

Источник каталога = адаптер, который умеет `search()` и `status()` —
того же контракта, что у JsonSource и официального индекса. Раньше
здесь жил второй, несовместимый контракт (`list_items`/`get_item`),
а admin.py звал `search()` — на живой сборке это давало
`AttributeError: 'NeuralDeepSource' object has no attribute 'search'`.

Карточки neuraldeep — установки вида `npx skillsbd add …`; агент
получает готовую команду, поэтому карточка отдаёт `command`/`args`.
"""
from __future__ import annotations

import re

import httpx

ND_BASE = "https://neuraldeep.ru"
ND_TIMEOUT = 8.0

# В RSC-потоке Next.js карточки лежат как escape-JSON. Достаём пары
# (тип, slug) из href-ов /mcp/<slug> и /skills/<slug>.
_HREF = re.compile(rb'\\?"\/(mcp|skills)\\?\/([a-z0-9-]+)\\?"')
_INSTALL = re.compile(r"npx\s+skillsbd\s+add\s+([A-Za-z0-9_@/.:-]+)")
_REPO = re.compile(r'github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)')


class NeuralDeepSource:
    """Каталог neuraldeep.ru под общим контрактом источников.

    `state` — результат последней попытки, а не константа: раньше
    источник светился зелёным, оставаясь нерабочим.
    """

    name = "neuraldeep"

    def __init__(self, base: str = ND_BASE, timeout: float = ND_TIMEOUT,
                 proxy: str | None = None):
        self.base = base
        self.timeout = timeout
        self.proxy = proxy
        self.state = "unprobed"
        self.error: str | None = None
        self._transport: httpx.BaseTransport | None = None
        self._cards: list[dict] | None = None

    # ---- сеть ----

    def _fetch(self, path: str) -> bytes:
        with httpx.Client(timeout=self.timeout, trust_env=False,
                          proxy=self.proxy, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0 AOMG/0.1"},
                          **({"transport": self._transport}
                             if self._transport is not None else {})) as c:
            r = c.get(f"{self.base}{path}")
            r.raise_for_status()
            return r.content

    def _load_slugs(self) -> list[tuple[str, str]]:
        html = self._fetch("/skills")
        seen: set[tuple[str, str]] = set()
        out: list[tuple[str, str]] = []
        for kind, slug in _HREF.findall(html):
            k, s = kind.decode(), slug.decode()
            if (k, s) in seen or s == "validator":
                continue
            seen.add((k, s))
            out.append((k, s))
        return out

    # ---- контракт источника ----

    def status(self) -> tuple:
        return self.state, self.error

    def search(self, query: str = "", limit: int = 50,
               offset: int = 0) -> dict:
        q = (query or "").strip().lower()
        try:
            if self._cards is None:
                self._cards = self._build_cards()
            self.state, self.error = "ok", None
        except Exception as ex:
            self.state = "unreachable"
            self.error = f"{type(ex).__name__}: {ex}"[:150]
            return {"items": [], "total": 0, "has_more": False}
        matched = [c for c in self._cards
                   if not q or q in c["name"].lower()
                   or q in (c.get("title") or "").lower()
                   or q in (c.get("description") or "").lower()]
        total = len(matched)
        page = matched[offset:offset + limit]
        for card in page:
            card.setdefault("source", self.name)
        return {"items": page, "total": total,
                "has_more": offset + len(page) < total}

    def _build_cards(self) -> list[dict]:
        cards: list[dict] = []
        for kind, slug in self._load_slugs():
            card = self._card_for(kind, slug)
            if card:
                cards.append(card)
        return cards

    def _card_for(self, kind: str, slug: str) -> dict | None:
        """Карточка каталога. Для /mcp/* стараемся достать команду
        установки; если страница недоступна — отдаём slug как имя."""
        card = {"name": slug, "title": slug.replace("-", " ").title(),
                "description": "", "kind": kind, "source": self.name,
                "url": None, "command": None, "args": [],
                "install": None, "form_fields": []}
        try:
            html = self._fetch(f"/{kind}/{slug}").decode("utf-8", "replace")
        except Exception:
            return card
        m = _INSTALL.search(html)
        if m:
            # страница отдаёт `npx skillsbd add <pkg>`; в args обязаны
            # попасть все три части, иначе панель подставит панели
            # `npx -y Rusender/...` — команду, которой не существует
            card["command"], card["args"] = "npx", ["-y", "skillsbd",
                                                   "add", m.group(1)]
            card["install"] = m.group(1)
        rm = _REPO.search(html)
        if rm:
            card["repo_url"] = f"https://github.com/{rm.group(1)}"
        text = re.sub(r"<[^>]+>", "\n", html)
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        # Ищем заголовок в обеих формах: `slug` как есть (rusender-mcp)
        # и с заменой дефисов на пробелы (rusender mcp) — на странице
        # встречается любая из двух, и строгое сравнение молча оставляло
        # карточку без описания.
        variants = {slug.lower(), slug.replace("-", " ").lower()}
        card["description"] = ""
        for i, l in enumerate(lines):
            if l.lower() in variants and i + 1 < len(lines):
                nxt = lines[i + 1]
                if len(nxt) > 20 and "npx " not in nxt:
                    card["description"] = nxt[:200]
                break
        return card
