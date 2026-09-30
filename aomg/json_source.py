"""Универсальный JSON-каталог (ADR-0002): GET url -> список серверов.

Формы ответа: [запись, ...] или {"servers": [запись, ...]}.
Запись: name обязателен, ровно одно из url (http) / command (stdio,
строка целиком — разбивается по пробелам). TTL-кэш на диске: свежий —
сеть не трогаем; источник недоступен — старый кэш + state=unreachable.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .config import CatalogSourceSpec

TTL = 24 * 3600.0
TIMEOUT = 10.0   # per-source таймаут: недоступный источник не тормозит UI


@dataclass
class FetchResult:
    items: list[dict] = field(default_factory=list)   # карточки admin api
    state: str = "ok"          # ok | error | unreachable
    error: str | None = None


def _entry_to_card(rec: dict) -> dict | None:
    name = (rec.get("name") or "").strip()
    if not name:
        return None
    url = rec.get("url")
    command = rec.get("command")
    args: list[str] = []
    if url and command:
        return None            # ровно одно из url/command
    if not url and command:
        parts = str(command).split()
        if not parts:
            return None
        command, args = parts[0], parts[1:]
    elif not url:
        return None
    return {
        "name": name,
        "title": rec.get("title") or name,
        "kind": "http" if url else "stdio",
        "url": url,
        "command": command,
        "args": args,
        "description": (rec.get("description") or "")[:140],
        "headers": rec.get("headers") or {},
        "env": rec.get("env") or {},
        "install": None,
        "form_fields": [],
    }


class JsonSource:
    """Один пользовательский источник типа json."""

    name: str

    def __init__(self, spec: CatalogSourceSpec, cache_dir: Path,
                 proxy: str | None = None):
        self.name = spec.name
        self.spec = spec
        self.cache_dir = cache_dir
        self.cache_path = cache_dir / f"catalog-cache-{spec.name}.json"
        # Прокси egress-группы источника (ADR-0005 I2). До этого
        # `group` в конфиге был декоративным: корп-каталог за VPN
        # не скачивался, а UI показывал «доступен».
        self.proxy = proxy
        self.state = "unprobed"
        self.error: str | None = None
        self._transport: httpx.BaseTransport | None = None  # для тестов
        self._mem: tuple[float, list[dict]] | None = None   # (ts, items)

    # ---- диск ----

    def _load_cache(self) -> tuple[float, list[dict]] | None:
        try:
            d = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return float(d["ts"]), d["items"]
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _save_cache(self, items: list[dict]) -> None:
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ts": time.time(), "items": items},
                                  ensure_ascii=False), encoding="utf-8")
        import os
        os.replace(tmp, self.cache_path)

    # ---- fetch + parse ----

    def _client(self) -> httpx.Client:
        """Клиент источника: egress-группа (ADR-0005 I2).

        Тонкость httpx: переданный `transport` при непустом `proxy`
        игнорируется — для прокси создаётся свой транспорт. Поэтому
        инъекция транспорта (тесты) идёт через mounts, иначе тест на
        прокси молча уходил бы в сеть.
        """
        if self._transport is not None:
            return httpx.Client(timeout=TIMEOUT, trust_env=False,
                                mounts={"all://": self._transport})
        return httpx.Client(timeout=TIMEOUT, trust_env=False,
                            proxy=self.proxy)

    def _fetch_network(self) -> object:
        with self._client() as c:
            r = c.get(self.spec.url, headers=self.spec.headers or {})
            r.raise_for_status()
            return r.json()

    def fetch(self, force: bool = False) -> FetchResult:
        """force=True: игнорировать свежий кэш (обновить)."""
        cached = self._load_cache()
        if not force and cached and time.time() - cached[0] < TTL:
            self.state, self.error = "ok", None
            return FetchResult(items=cached[1], state="ok")
        try:
            data = self._fetch_network()
        except Exception as ex:
            if cached:
                self.state = "unreachable"
                self.error = str(ex)[:150]
                return FetchResult(items=cached[1], state="unreachable",
                                   error=self.error)
            self.state = "error"
            self.error = str(ex)[:150]
            return FetchResult(items=[], state="error", error=self.error)
        if isinstance(data, dict):
            data = data.get("servers")
        rows = data if isinstance(data, list) else None
        if rows is None:
            self.state = "error"
            self.error = "unexpected JSON shape"
            return FetchResult(items=[], state="error", error=self.error)
        items: list[dict] = []
        seen: set[str] = set()
        for rec in rows:
            if not isinstance(rec, dict):
                continue
            card = _entry_to_card(rec)
            if card and card["name"] not in seen:
                seen.add(card["name"])
                items.append(card)
        self.state, self.error = "ok", None
        self._save_cache(items)
        return FetchResult(items=items, state="ok")

    def status(self) -> tuple:
        """(state, error) — результат последней реальной попытки.

        До первой попытки честное `unprobed`, а не `ok`: зелёный свет
        у непроверенного источника — тот самый баг из аудита.
        """
        return self.state, self.error

    def search(self, query: str = "", limit: int = 50,
               offset: int = 0) -> dict:
        """Поиск по последнему известному набору карточек (кэш/сеть).

        Возвращает {items, total, has_more} — общий контракт источников
        каталога (ADR-0005). offset даёт срез, а не первые N.
        """
        res = self.fetch()
        q = (query or "").strip().lower()
        matched: list[dict] = []
        for card in res.items:
            if not q or q in card["name"].lower() \
                    or q in (card.get("title") or "").lower() \
                    or q in (card.get("description") or "").lower():
                matched.append(card)
        total = len(matched)
        page = matched[offset:offset + limit]
        for card in page:
            card.setdefault("source", self.name)
        return {"items": page, "total": total,
                "has_more": offset + len(page) < total}
