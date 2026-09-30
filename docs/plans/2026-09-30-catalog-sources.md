# Каталог из нескольких источников — Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Поиск MCP-серверов по нескольким каталогам (официальный реестр, neuraldeep, пользовательские JSON-источники за VPN/шлюзом) с per-source табами и бейджами доступности.

**Architecture:** Поверх существующего `CatalogSource` (aomg/catalog.py) — реестр источников в конфиге (`catalog_sources`), универсальный JsonSource с TTL-дисковым кэшем на источник, API `/admin/api/catalog/sources` + параметр `source` в поиске, UI с табами. Встроенные источники: official (индекс), neuraldeep (адаптер).

**Tech Stack:** Python 3.11, FastAPI, httpx, pytest. Конфиг — существующий yaml с `${VAR}`-подстановкой.

**Spec:** docs/SPEC-catalog-sources.md · **ADR:** docs/adr/ADR-0002-multi-source-catalog.md

---

## Phase 1: Модель источников и JSON-адаптер (backend, без UI)

### Task 1.1: Модель CatalogSourceSpec в config.py

**Objective:** Источники каталога читаются из конфига.

**Files:**
- Modify: `aomg/config.py` (добавить dataclass + парсинг секции)
- Modify: `aomg/catalog.py` (реестр SOURCES станет динамическим — в 1.3)
- Test: `tests/test_catalog_sources.py` (создать)

**Step 1: Write failing test**

```python
# tests/test_catalog_sources.py
def test_config_parses_catalog_sources(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "gateway_port: 9300\n"
        "groups:\n  vpn: {name: VPN, proxy: 'socks5://127.0.0.1:1'}\n"
        "servers: {}\n"
        "catalog_sources:\n"
        "  corp:\n    type: json\n"
        "    url: https://corp-catalog.example/api/v1/mcp-servers\n"
        "    group: vpn\n"
        "    headers: {Authorization: 'Bearer ${CORP_CATALOG_TOKEN}'}\n"
        "  neuraldeep: {hidden: false}\n",
        encoding="utf-8")
    from aomg.config import load_config
    cfg = load_config(cfg_file)
    src = cfg.catalog_sources["corp"]
    assert src.type == "json"
    assert src.group == "vpn"
    assert src.url.endswith("mcp-servers")
    assert cfg.catalog_sources["neuraldeep"].hidden is False

def test_catalog_sources_section_optional(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("gateway_port: 9300\nservers: {}\n", encoding="utf-8")
    from aomg.config import load_config
    cfg = load_config(cfg_file)
    assert cfg.catalog_sources == {}
```

**Step 2: RED** — `pytest tests/test_catalog_sources.py -v` → AttributeError: no attribute 'catalog_sources'

**Step 3: Implement** — в `config.py`:

```python
@dataclass
class CatalogSourceSpec:
    name: str
    type: str = "json"            # json | (встроенные обрабатываются отдельно)
    url: str = ""
    group: str = "direct"
    headers: dict[str, str] = field(default_factory=dict)
    hidden: bool = False
```

В `Config`: `catalog_sources: dict[str, CatalogSourceSpec] = field(default_factory=dict)`.
В `load_config`, после servers:

```python
    for name, s in (raw.get("catalog_sources") or {}).items():
        if not isinstance(s, dict):
            continue
        spec = CatalogSourceSpec(
            name=name, type=s.get("type", ""),
            url=s.get("url", ""), group=s.get("group", "direct"),
            headers={k: _expand(v) if isinstance(v, str) else v
                     for k, v in (s.get("headers") or {}).items()},
            hidden=bool(s.get("hidden", False)))
        if not spec.type and name != "neuraldeep":
            continue  # неизвестное имя без type: warning + пропуск
        cfg.catalog_sources[name] = spec
```

**Step 4: GREEN** — тот же pytest → PASS.

**Step 5: Commit** — `git commit -m "feat(catalog): CatalogSourceSpec in config"`

### Task 1.2: JsonSource — универсальный HTTP-каталог с TTL-кэшем

**Objective:** JSON-манифест (обе формы) → карточки; кэш на диске; недоступен → старый кэш + state.

**Files:**
- Create: `aomg/json_source.py`
- Test: `tests/test_json_source.py` (создать)

**Step 1: Write failing tests**

```python
# tests/test_json_source.py
LIST_A = [{"name": "blender-mcp", "title": "Blender MCP",
           "description": "Blender integration",
           "url": "https://x.example/blender/mcp"},
          {"name": "unreal-mcp", "command": "npx -y unreal-mcp@2"}]
LIST_B = {"servers": LIST_A}

def _mk(tmp_path, payload):
    from aomg.json_source import JsonSource
    from aomg.config import CatalogSourceSpec
    spec = CatalogSourceSpec(name="corp", type="json",
                             url="https://corp.example/mcp-servers")
    src = JsonSource(spec, cache_dir=tmp_path)
    # подмена транспорта — через httpx.MockTransport в реализации fetch
    return src

def test_parses_list_form(tmp_path, monkeypatch): ...
    # JsonSource получает httpx.MockTransport([[200, LIST_A]]) — см. impl
def test_parses_wrapped_form(...): ...
def test_command_split_into_command_args(...): ...
    # карточка command "npx -y unreal-mcp@2" → command="npx", args=["-y","unreal-mcp@2"]
def test_bad_format_marks_source_error(...): ...
    # ответ {"hello": "world"} → state="error", items=[], error-сообщение
def test_ttl_cache_no_network_when_fresh(...): ...
def test_unreachable_serves_stale_and_marks(...): ...
    # первый fetch ok → кэш; транспорт сломан → items из кэша, state="unreachable"
```

**Step 2: RED** — модуля нет → ImportError.

**Step 3: Implement** `aomg/json_source.py`:

```python
"""Универсальный JSON-каталог (ADR-0002): GET url -> список серверов.

Формы: [запись...] или {"servers": [запись...]}. Запись: name обязателен,
ровно одно из url/command. TTL-кэш на диске: свежий -- сеть не трогаем,
недоступен источник -- старый кэш + state=unreachable.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import CatalogSourceSpec
from .registry import RegistryEntry

TTL = 24 * 3600.0
TIMEOUT = 10.0   # per-source таймаут: недоступный источник не тормозит UI


@dataclass
class FetchResult:
    items: list[dict]          # карточки в формате admin api
    state: str                 # ok | error | unreachable
    error: str | None = None


def _entry_to_card(rec: dict) -> dict | None:
    name = (rec.get("name") or "").strip()
    if not name:
        return None
    url, command, args = rec.get("url"), rec.get("command"), []
    if not url and not command:
        return None
    if command and not url:
        parts = str(command).split()
        command, args = parts[0], parts[1:]
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
    name: str

    def __init__(self, spec: CatalogSourceSpec, cache_dir: Path):
        self.name = spec.name
        self.spec = spec
        self.cache_path = cache_dir / f"catalog-cache-{spec.name}.json"
        self.state = "ok"
        self.error: str | None = None

    # ---- кэш ----
    def _load_cache(self) -> tuple[float, list[dict]] | None:
        try:
            d = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return float(d["ts"]), d["items"]
        except (OSError, ValueError, KeyError):
            return None

    def _save_cache(self, items: list[dict]) -> None:
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ts": time.time(), "items": items},
                                  ensure_ascii=False), encoding="utf-8")
        import os
        os.replace(tmp, self.cache_path)

    # ---- fetch + parse ----
    def fetch(self, transport: httpx.BaseTransport | None = None) -> FetchResult:
        cached = self._load_cache()
        try:
            with httpx.Client(timeout=TIMEOUT, trust_env=False,
                              transport=transport) as c:
                r = c.get(self.spec.url, headers=self.spec.headers or {})
                r.raise_for_status()
                data = r.json()
        except Exception as ex:
            if cached:
                self.state, self.error = "unreachable", str(ex)[:150]
                return FetchResult(items=cached[1], state="unreachable",
                                   error=str(ex)[:150])
            self.state, self.error = "error", str(ex)[:150]
            return FetchResult(items=[], state=self.state,
                               error=self.error)
        if isinstance(data, dict):
            data = data.get("servers")
        rows = data if isinstance(data, list) else None
        if rows is None:
            self.state, self.error = "error", "unexpected JSON shape"
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
```

**Примечание к тестам кэша:** для «свежий кэш — сеть не трогаем» используй
`httpx.MockTransport` со счётчиком запросов; для «unreachable → stale» —
транспорт, бросающий `httpx.ConnectError`.

**Step 4: GREEN**, **Step 5: Commit** — `feat(catalog): JsonSource with TTL disk cache`

### Task 1.3: Динамический реестр источников + API

**Objective:** `/admin/api/catalog/sources` (список+state), поиск с `?source=`,
POST/DELETE источников, чтение конфиг-секции при старте admin.

**Files:**
- Modify: `aomg/admin.py` (register_admin: источники; api_catalog: параметр source)
- Test: `tests/test_admin_sources_api.py` (создать; FastAPI TestClient,
  приложение собирается как в существующих e2e — см. tests/test_e2e.py)

**Step 1: Write failing tests**

```python
# tests/test_admin_sources_api.py
# - GET /admin/api/catalog/sources -> [{"name":"official",...},{"name":"neuraldeep",...}]
#   official всегда первый; neuraldeep с hidden из конфига
# - GET /admin/api/catalog?source=corp&query=blender с подменённым транспортом
#   -> items из json-источника, каждая карточка имеет "source":"corp"
# - GET /admin/api/catalog?query=github (без source) -> ищет в official (совместимость)
# - источник с url на закрытый порт -> state="unreachable", ответ не висит >10s
# - POST /admin/api/catalog/sources пишет в yaml (headers с секретом маскируются
#   в ответе GET), DELETE удаляет; builtin нельзя
```

**Step 2: RED**, **Step 3: Implement** в admin.py:

- при `register_admin` собрать `catalog_sources_runtime`:
  `{"official": OfficialIndexAdapter(...), "neuraldeep": NeuralDeepSource(...),
  **{name: JsonSource(spec, cache_dir=config_path.parent)
     for name, spec in cfg.catalog_sources.items() if spec.type == "json"}}`.
  `OfficialIndexAdapter` — тонкая обёртка: `search(query)` → существующий
  `CatalogIndex(config_path.parent / "registry-index.json").search(query)`
  + `ensure_fresh()`; карточкам проставить `source: "official"`.
- `GET /admin/api/catalog/sources`: для каждого — `{name, type, url?, group,
  hidden, state, error}`; state берётся у объекта источника (для official —
  по возрасту индекса: fresh→ok, stale→ok (обновляется), пусто→error).
- `api_catalog(query, source: str = "official")`: выбор источника из словаря;
  neuraldeep: фильтрация его `list_items()` по подстроке (как было);
  JsonSource: `fetch()` + локальный фильтр по подстроке в name/title/description
  (чтобы не рефетчить на каждый символ — фильтр по последнему fetch-результату
  с TTL 60с в памяти).
- POST/DELETE: валидация (имя slug, type=json только), запись в `raw_config()`
  → `save_raw` → перечитать `cfg.catalog_sources` (как делает api_upsert),
  пересоздать объект источника; секреты в ответе маскируются `_is_secret`.

**Step 4: GREEN**, **Step 5: Commit** — `feat(catalog): per-source search API`

## Phase 2: UI — табы источников

### Task 2.1: Каталог-блок с табами в админке

**Objective:** Блок «Каталог»: табы per-source, поиск в выбранном, бейджи
состояния; диалог добавления сервера получает карточку из любого источника.

**Files:**
- Modify: `aomg/admin.py` (`_PAGE`: HTML/CSS/JS блока каталога)
- Verify: ручной смоук `hermes -p <профиль> mcp test` не нужен (это UI),
  проверка через `browser_navigate` на живой панели + скриншот.

**Steps:**
1. CSS: `.cat-tabs`, `.cat-tab`, `.cat-tab .dot`, `.cat-tab.active`,
   бейдж source на карточке.
2. JS: `loadSources()` → табы из `/admin/api/catalog/sources`;
   `switchSource(name)` → перерисовать результаты; поиск шлёт
   `&source=<name>`; карточка несёт `data-source`.
3. Диалог «Источники»: таблица (имя, тип, url, группа, состояние) +
   форма добавления (имя, url, группа-селектор из GROUPS, header key/value);
   POST/DELETE на новые эндпоинты; секретные значения — `type=password`.
4. Диалог добавления сервера: поле «Найти в каталоге» заменить на кнопку
   «Найти в каталоге…» → открывает блок каталога рядом; клик по карточке —
   `pick(card)` с предзаполнением (учесть, что карточка json-источника
   может нести `command`+`args` целиком — прокинуть в форму).

**Verify:** `browser_navigate` на `http://127.0.0.1:9300/admin` с поднятым
гейтвемом; скриншоты: табы, поиск в official, поиск в json-источнике
(фейковый на `http://127.0.0.1:<port>/mcp-servers.json` через отдельный
uvicorn в тестовом окружении), красный бейдж у недоступного источника.

**Commit** — `feat(admin): catalog tabs per source`

### Task 2.2: Дефолты каталога в конфиге (задача юзера)

**Objective:** В `config.example.yaml` — секция `catalog_sources` с
neuraldeep (не hidden) и закомментированным примером corp-источника
(корпоративный JSON-каталог за VPN) с `${CORP_CATALOG_TOKEN}` и `group: vpn`.

**Files:** Modify: `config.example.yaml`, `README.md` (раздел «Каталог» —
подраздел «Свои источники»), `docs/SDD.md` (компоненты + критерии 7b).

**Verify:** `load_config("config.example.yaml")` в python -c — парсится.

**Commit** — `feat(config): catalog_sources defaults`

## Phase 3: Версия, релиз

### Task 3.1: Версия 0.2.0

**Files:** `installer.iss` (`#define MyAppVersion "0.2.0"`),
`docs/SDD.md` (заголовок v0.2), README (релизная ссылка — после публикации).

### Task 3.2: Сборка + полный цикл проверки

1. pytest полный (сетевой тест — как обычно, отдельно).
2. `PyInstaller AOMG.spec` + `mcpproxy.spec` (onedir), копия mcp-proxy.exe
   в dist/AOMG/.
3. ISCC installer.iss → dist/installer/AOMG-setup-0.2.0.exe.
4. Живой цикл: тихая установка → запуск без --config → health →
   каталог: таб official + json-источник на локальном фейк-сервере →
   uninstall.
5. Коммит, пуш на оба хостинга (по «ок»), gh release v0.2.0 с
   инсталлятором + SHA256, заметки.

---

## Чеклист приёмки (гейт финальной проверки, не только RED→GREEN)

- [ ] Конфиг-совместимость: старый config.yaml без секции — поведение не изменилось.
- [ ] Per-source изоляция: недоступный corp-источник (закрытый порт) — бейдж
      красный ≤10с, official и neuraldeep ищут как обычно.
- [ ] Секреты: POST источника с Authorization → GET sources возвращает `***`.
- [ ] Перезапуск AOMG: источники из конфига живы после рестарта (persist).
- [ ] JSON-формы A и B парсятся; битый JSON → state=error, UI не падает.
- [ ] TTL-кэш: при недоступном источнике карточки берутся из дискового кэша.
- [ ] UI: табы переключаются, поиск в каждом источнике, карточка из
      не-official источника корректно добавляет сервер (url- и command-варианты).
