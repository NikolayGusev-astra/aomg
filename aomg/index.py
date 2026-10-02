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
PAGE_ATTEMPTS = 3          # попыток на одну страницу: реестр иногда
                           # не отвечает на конкретный cursor

_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _WORD.findall((text or "").lower())


class CatalogIndex:
    """Дисковый индекс реестра с фоновым обновлением."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.syncing = False
        self.last_sync_error: str | None = None
        self._sync_thread: threading.Thread | None = None
        self._mem: tuple | None = None      # (mtime, data) — кэш load()

    # ---- диск ----

    SENTINEL_BYTES = 160

    def _sentinel(self) -> str:
        """Первые байты файла — дешёвый «отпечаток» содержимого.

        Ключ кэша по `mtime_ns`/`size` ненадёжен: две записи подряд
        (слив реестра + тест, или две подряд заливки) на этой ФС получают
        ОДИНАКОВЫЙ mtime_ns, и кэш отдавал протухшие данные. При этом
        `synced_at` стоит в начале JSON, поэтому 160 байт достаточно,
        чтобы заметить перезапись, не разбирая 2.4 МБ.
        """
        try:
            with self.path.open("rb") as f:
                return f.read(self.SENTINEL_BYTES).decode("utf-8", "replace")
        except OSError:
            return ""

    def load(self) -> dict | None:
        """Разбор index.json с кэшем разбора.

        Поиск в UI дёргается на каждый keystroke, а файл весит ~2.4 МБ
        на 2779 записей — 33-48 мс чтения+парсинга на запрос (ADR-0005).
        Кэш ключуется по (mtime_ns, size, sentinel): только чтение первых
        160 байт на попытку, полный json.loads — при реальном изменении.
        """
        try:
            st = self.path.stat()
        except OSError:
            self._mem = None
            return None
        key = (st.st_mtime_ns, st.st_size, self._sentinel())
        if self._mem is not None and self._mem[0] == key:
            return self._mem[1]
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "servers" in data:
                self._mem = (key, data)
                return data
        except (OSError, ValueError):
            pass
        self._mem = None
        return None

    @property
    def cached(self) -> bool:
        return self._mem is not None

    def invalidate(self) -> None:
        self._mem = None

    def _save(self, rows: list[dict]) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"synced_at": time.time(),
                                   "servers": rows}, ensure_ascii=False),
                       encoding="utf-8")
        import os
        os.replace(tmp, self.path)
        self.invalidate()      # файл изменился — старый кэш неверен

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

        Отдельная страница может не отвечать — реестр роняет запрос по
        некоторым курсорам (наблюдалось на `ai.borealhost/mcp:0.4.2`).
        Раньше такая страница убивала весь sync и индекс оставался пустым
        при доступном реестре. Теперь страница повторяется ограниченное
        число раз, затем пропускается, а уже собранные записи
        сохраняются.

        Бросает исключение, только если не удалось получить ни одной
        страницы И на диске ничего нет; при живом старом индексе
        ошибку глотаем — индекс остаётся как был.
        """
        old = self.load()
        rows: list[dict] = []
        seen: set[str] = set()
        cursor: str | None = ""
        pages = 0
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0),
                          trust_env=False) as c:
            for _ in range(MAX_PAGES):
                params: dict = {"limit": PAGE_LIMIT}
                if cursor:
                    params["cursor"] = cursor
                batch, nxt = [], None
                for _attempt in range(PAGE_ATTEMPTS):
                    try:
                        r = c.get(f"{REGISTRY_BASE}/v0/servers", params=params)
                        r.raise_for_status()
                        body = r.json()
                        batch = body.get("servers", []) or []
                        nxt = (body.get("metadata", {}) or {}).get("nextCursor")
                        break
                    except Exception:
                        continue
                pages += 1
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
                if not nxt or not batch:
                    # нет курсора — конец; сорванная страница тоже конец:
                    # продолжать с того же места бессмысленно
                    break
                cursor = nxt
        if not rows and old is None:
            raise RuntimeError(f"registry returned no servers "
                               f"(pages={pages})")
        if rows:
            self._save(rows)
            return len(rows)
        return len(old["servers"]) if old else 0

    def ensure_fresh(self) -> bool:
        """Фоново обновить индекс, если устарел. False = уже идёт sync.

        Ошибка не глотается молча: она попадает в last_sync_error, чтобы
        панель могла сказать «реестр недоступен», а не «ничего не
        нашлось» (ADR-0005 — правдивый статус).
        """
        with self.lock:
            if self.syncing:
                return False
            if not self.is_stale():
                return False
            self.syncing = True

        def run():
            try:
                self.sync()
                self.last_sync_error = None
            except Exception as e:
                # старый индекс остаётся; причина видна панели
                self.last_sync_error = f"{type(e).__name__}: {e}"
            finally:
                self.syncing = False
        self._sync_thread = threading.Thread(target=run, daemon=True,
                                             name="aomg-ensure-fresh")
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


    def age(self) -> float | None:
        """Сколько секунд прошло с последнего успешного sync, None - нет."""
        data = self.load()
        if not data:
            return None
        return max(0.0, time.time() - float(data.get("synced_at") or 0))

    def is_stale(self, max_age: float = STALE_AFTER) -> bool:
        """Индекс старше max_age секунд (или его нет вовсе).

        Панель показывает это честно, а не отдаёт вчерашние данные
        как свежие: раньше official выглядел пустым 5 минут после
        старта, и это приходилось объяснять как норму.
        """
        a = self.age()
        return a is None or a > max_age


def read_mcp_requirement(metadata: dict | None) -> str | None:
    """Минимальная версия mcp из Requires-Dist установленного пакета.

    Нужна импортёру: сервер может требовать `mcp>=1.20.0` (Icon, meta=),
    а в общем venv стоит 1.9.4 - падение выглядело бы как вина гейтвея.
    """
    for req in (metadata or {}).get("Requires-Dist") or []:
        m = re.match(r"\s*mcp\s*>=\s*([\d.]+)", str(req))
        if m:
            return m.group(1)
    return None


class IndexSync:
    """Фоновая автосинхронизация реестра.

    Раньше `sync()` звался только вручную, поэтому после перезапуска
    official оставался пустым на все время обхода (~270 с на 2794
    записях). Здесь sync идёт в отдельном потоке, а панель может
    показать его состояние, не блокируя UI.
    """

    def __init__(self, index: CatalogIndex, interval: float = 6 * 3600.0,
                 timeout: float = 60.0, enabled: bool = True,
                 max_age: float = STALE_AFTER):
        self.index = index
        self.interval = interval
        self.timeout = timeout
        self.enabled = enabled
        self.max_age = max_age
        self.last_count: int = 0
        self.last_error: str | None = None
        self.last_run: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self) -> bool:
        if not self.enabled or self._thread is not None:
            return self.enabled
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="aomg-index-sync")
        self._thread.start()
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            # индекс протух - тогда точно идём; свежий - ждём интервал
            if self.index.is_stale(self.max_age):
                self.run_once()
            self._ready.set()
            self._stop.wait(self.interval)

    def run_once(self) -> int:
        try:
            n = self.index.sync(timeout=self.timeout)
            self.last_count, self.last_error = n, None
        except Exception as e:      # реестр недоступен - не теряем старый
            self.last_error = f"{type(e).__name__}: {e}"
        finally:
            self.last_run = time.time()
        return self.last_count

    def wait_ready(self, timeout: float = 5.0) -> bool:
        return self._ready.wait(timeout)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def status(self) -> dict:
        """Состояние для панели: чем занят индекс и почему пусто."""
        return {"count": self.last_count, "error": self.last_error,
                "last_run": self.last_run, "age": self.index.age(),
                "running": self._thread is not None}
