"""Локальный индекс каталога: диск-кэш реестра + мгновенный поиск.

Зачем: официальный реестр отвечает от 0.5 до ~45 секунд (бывает хуже),
ходить в него на каждое нажатие клавиши нельзя. Вместо этого:

- ``sync()`` один раз стягивает весь список серверов (/v0/servers c
  cursor-пагинацией) и кладёт на диск (index.json рядом с config.yaml).
- ``search()`` ищет ПО ИНДЕКСУ на диске — мгновенно, без сети.
- Индекс обновляется, только если ему больше ``STALE_AFTER`` часов.
- Упавший реестр не портит диск: старый индекс живёт до успешного sync.

Формат index.json: {"synced_at": ts, "servers": [RegistryEntry-совместимые
dicts]}. Дедуп по короткому имени (split("/")[-1]) делается здесь же —
один пакет живёт под несколькими namespace.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import httpx

from .registry import REGISTRY_BASE, parse_server_entry

STALE_AFTER = 24 * 3600.0     # секунды; старше — индекс пора обновить
PAGE_LIMIT = 100              # размер страницы /v0/servers
MAX_PAGES = 100               # предохранитель от бесконечного cursor

_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _WORD.findall((text or "").lower())


class CatalogIndex:
    """Дисковый индекс реестра с фоновым обновлением."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.syncing = False
        self._sync_thread: threading.Thread | None = None

    # ---- диск ----

    def load(self) -> dict | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "servers" in data:
                return data
        except (OSError, ValueError):
            pass
        return None

    def _save(self, rows: list[dict]) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"synced_at": time.time(),
                                   "servers": rows}, ensure_ascii=False),
                       encoding="utf-8")
        import os
        os.replace(tmp, self.path)

    # ---- sync ----

    def age(self) -> float | None:
        data = self.load()
        if not data:
            return None
        return time.time() - float(data.get("synced_at") or 0)

    def is_stale(self) -> bool:
        age = self.age()
        return age is None or age > STALE_AFTER

    def sync(self, timeout: float = 60.0) -> int:
        """Полный слив реестра на диск. Возвращает число записей.

        Бросает исключение, если реестр недоступен И на диске ничего нет;
        при живом старом индексе ошибку глотаем — индекс остаётся как был.
        """
        old = self.load()
        rows: list[dict] = []
        seen: set[str] = set()
        cursor: str | None = ""
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0),
                          trust_env=False) as c:
            for _ in range(MAX_PAGES):
                params: dict = {"limit": PAGE_LIMIT}
                if cursor:
                    params["cursor"] = cursor
                r = c.get(f"{REGISTRY_BASE}/v0/servers", params=params)
                r.raise_for_status()
                batch = r.json().get("servers", [])
                for row in batch:
                    if not isinstance(row, dict):
                        continue
                    srv = row.get("server") or {}
                    name = srv.get("name") or ""
                    short = name.split("/")[-1]
                    if name in seen or short in seen:
                        continue          # дедуп: один пакет под разными ns
                    seen.add(name)
                    seen.add(short)
                    rows.append(row)
                cursor = r.json().get("metadata", {}).get("nextCursor")
                if not cursor or not batch:
                    break
        if not rows and old is None:
            raise RuntimeError("registry returned no servers")
        if rows:
            self._save(rows)
            return len(rows)
        return len(old["servers"]) if old else 0

    def ensure_fresh(self) -> bool:
        """Фоново обновить индекс, если устарел. False = уже идёт sync."""
        with self.lock:
            if self.syncing:
                return False
            if not self.is_stale():
                return False
            self.syncing = True
        def run():
            try:
                self.sync()
            except Exception:
                pass  # старый индекс остаётся; попробуем в следующий раз
            finally:
                self.syncing = False
        self._sync_thread = threading.Thread(target=run, daemon=True)
        self._sync_thread.start()
        return True

    # ---- поиск ----

    def search(self, query: str, limit: int = 20) -> list[dict]:
        """Поиск по локальному индексу: подстрока в имени/описании,
        при пустом запросе — первые limit записей.

        Возвращает items в формате карточек админки
        ({name, title, kind, url, description, install, form_fields}).
        """
        data = self.load()
        if not data:
            return []
        q = (query or "").strip().lower()
        qtok = _tokens(q)
        scored: list[tuple[float, dict]] = []
        for row in data.get("servers", []):
            try:
                e = parse_server_entry(row)
            except ValueError:
                continue  # inactive/deprecated — в индексе они тоже есть
            hay = f"{(e.name or '').split('/')[-1]} {e.title} {e.description}".lower()
            # namespace (io.github.*) в хей не попадает: иначе запрос
            # 'github' матчится с каждой второй записью реестра
            if qtok:
                # все токены запроса должны найтись; позиция имени весит больше
                if not all(t in hay for t in qtok):
                    continue
                score = 0.0
                name_l = (e.name or "").lower()
                for t in qtok:
                    if t in name_l:
                        score += 2.0
                score += 0.1 * len(qtok)
            else:
                score = 0.0
            if e.kind == "http" and e.remote_url:
                url, install = e.remote_url, None
            elif e.package:
                url, install = None, e.package.get("identifier")
            else:
                continue
            scored.append((score, {
                "name": (e.name or "").split("/")[-1],
                "title": e.title, "kind": e.kind, "url": url,
                "description": (e.description or "")[:140],
                "install": install,
                "form_fields": e.form_fields}))
        scored.sort(key=lambda x: -x[0])
        return [item for _, item in scored[:limit]]


def warm(index: CatalogIndex) -> None:
    """Разовый прогрев при старте: пусть индекс свежий к открытию админки."""
    try:
        index.ensure_fresh()
    except Exception:
        pass
