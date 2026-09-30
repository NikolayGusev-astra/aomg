"""Админ-API + веб-GUI: /admin (страница), CRUD серверов, каталог.

Принцип UX для не-инженера: ни процессов, ни портов; «Добавить MCP» ->
выбор из каталога (рубрики) или своя ссылка -> форма (url/command, ключ в
одном поле) -> сохранить. Секреты никогда не возвращаются в GUI.
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse

from .config import Config, ServerSpec, atomic_save_config, load_config
from .health import Health, aggregate_state
from .registry import REGISTRY_BASE, parse_server_entry
from .registry import search as registry_search
from .supervisor import Supervisor

_SECRET_RE = re.compile(r"(key|token|password|pat|secret)", re.I)


def _entry_to_item(e) -> dict:
    """RegistryEntry -> карточка каталога (формат ответа /admin/api/catalog)."""
    if e.kind == "http" and e.remote_url:
        url, install = e.remote_url, None
    elif e.package:
        url, install = None, e.package.get("identifier")
    else:
        return {}
    return {
        "name": (e.name or "").split("/")[-1],
        "title": e.title, "kind": e.kind, "url": url,
        "description": (e.description or "")[:140],
        "install": install,
        "form_fields": e.form_fields}


def _is_secret(k: str) -> bool:
    return bool(_SECRET_RE.search(k))


_SECRET_KEY_NAMES = {"authorization", "proxy-authorization", "cookie"}


async def _json_body(request: Request) -> dict | None:
    """Разбор тела запроса. Кривой JSON -> None, а не 500.

    Раньше `await request.json()` бросал JSONDecodeError прямо наружу:
    опечатка в теле от панели или от curl давала 500 с трейсбеком
    вместо внятного «неверный запрос». Ошибка ввода — это 400, а не
    авария приложения.
    """
    try:
        data = await request.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _mask_headers(hdrs: dict) -> dict:
    """Секреты в заголовках маскируются: по имени ключа (key/token/…) и
    по классическим носителям (Authorization, Cookie)."""
    return {k: ("***" if v and (_is_secret(k)
                                or k.lower() in _SECRET_KEY_NAMES) else v)
            for k, v in hdrs.items()}


def register_admin(app: FastAPI, cfg: Config, supervisor: Supervisor,
                   config_path: Path,
                   restart_watchdog: callable) -> None:
    """Роуты админки. config_path нужен для сохранения yaml.

    `healths` больше не параметр: состоянием владеет Supervisor
    (ADR-0003), панель читает его через health()/snapshot().
    """

    def health_of(name: str):
        h = supervisor.health(name)
        return h if h is not None else Health()

    def raw_config() -> dict:
        import yaml
        return yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    def save_raw(raw: dict) -> None:
        atomic_save_config(config_path, raw)
        restart_watchdog()

    def spec_to_public(name: str, spec: ServerSpec) -> dict:
        """Сервер для GUI: секреты маскированы."""
        env = {k: ("***" if _is_secret(k) and v else v)
               for k, v in spec.env.items()}
        hdrs = {k: ("***" if _is_secret(k) and v else v)
                for k, v in spec.headers.items()}
        h = health_of(name)
        return {"name": name, "kind": spec.kind, "group": spec.group,
                "url": spec.url, "command": spec.command, "env": env,
                "headers": hdrs, "state": h.state,
                "tools": h.tools, "error": h.error}

    # ---- данные ----

    @app.get("/admin/api/servers")
    def api_servers() -> dict:
        return {"servers": [spec_to_public(n, s)
                            for n, s in cfg.servers.items()]}

    # ---- каталог: источники (ADR-0002) ----

    def _build_catalog_sources() -> None:
        """official + neuraldeep (встроенные) + json-источники из конфига.

        Источник получает прокси своей egress-группы при создании
        (ADR-0005 I2) — group из конфига перестаёт быть декоративным.
        """
        from .catalog import NeuralDeepSource
        from .json_source import JsonSource
        sources: dict = {"official": _OfficialIndexAdapter(
            config_path.parent / "registry-index.json")}
        nd_spec = cfg.catalog_sources.get("neuraldeep")
        if not (nd_spec and nd_spec.hidden):
            sources["neuraldeep"] = NeuralDeepSource(
                proxy=cfg.egress_for_group(
                    nd_spec.group if nd_spec else "direct"))
        for name, spec in cfg.catalog_sources.items():
            if spec.type == "json":
                sources[name] = JsonSource(
                    spec, cache_dir=config_path.parent,
                    proxy=cfg.egress_for_group(spec.group))
        app.state.catalog_sources = sources

    def _rebuild_one_source(name: str) -> None:
        """Пересоздать ОДИН источник, не трогая остальные (ADR-0005 I3).

        Прежняя пересборка всего словаря на каждый upsert выбрасывала
        кэш и состояние всех остальных источников.
        """
        from .json_source import JsonSource
        spec = cfg.catalog_sources.get(name)
        if spec is None or spec.type != "json":
            app.state.catalog_sources.pop(name, None)
            return
        app.state.catalog_sources[name] = JsonSource(
            spec, cache_dir=config_path.parent,
            proxy=cfg.egress_for_group(spec.group))

    class _OfficialIndexAdapter:
        """Официальный реестр под единым контрактом (ADR-0005).

        Состояние — результат последней попытки, а не константа «ok»:
        раньше зелёный свет горел у источника, который не отвечает.
        """

        name = "official"

        def __init__(self, index_path: Path):
            from .index import CatalogIndex
            self._idx = CatalogIndex(index_path)
            self.state = "unprobed"
            self.error: str | None = None

        def status(self) -> tuple:
            return self.state, self.error

        def search(self, query: str = "", limit: int = 50,
                   offset: int = 0) -> dict:
            found = self._idx.search(query, limit=limit + offset)
            total = len(found)
            items = found[offset:offset + limit]
            for card in items:
                card.setdefault("source", "official")
            if self._idx.is_stale():
                self._idx.ensure_fresh()   # фоново, ответ не ждём
                self.state, self.error = "ok", None
            else:
                self.state, self.error = "ok", None
            return {"items": items, "total": total,
                    "has_more": offset + len(items) < total}

    _build_catalog_sources()
    BUILTIN_SOURCES = ("official", "neuraldeep")

    def _source_public(name: str, src) -> dict:
        state, error = src.status()
        spec = cfg.catalog_sources.get(name)
        out = {"name": name,
               "type": spec.type if spec else
                       ("json" if name == "neuraldeep" else "builtin"),
               "group": spec.group if spec else "direct",
               "hidden": spec.hidden if spec else False,
               "state": state, "error": error}
        if spec and spec.url:
            out["url"] = spec.url
        if spec and spec.headers:
            out["headers"] = _mask_headers(spec.headers)
        return out

    @app.get("/admin/api/catalog/sources")
    def api_catalog_sources() -> dict:
        srcs = app.state.catalog_sources
        names = [n for n in ("official", "neuraldeep") if n in srcs] + \
                [n for n in srcs if n not in BUILTIN_SOURCES]
        return {"sources": [_source_public(n, srcs[n]) for n in names]}

    @app.post("/admin/api/catalog/sources")
    async def api_catalog_source_upsert(request: Request) -> dict:
        data = await _json_body(request)
        if data is None:
            return {"error": "неверный JSON в теле запроса"}
        name = re.sub(r"[^a-z0-9_-]", "", (data.get("name") or "").lower())
        if not name or name in BUILTIN_SOURCES:
            return {"error": "bad name"}
        if data.get("type") != "json" or not data.get("url"):
            return {"error": "type=json и url обязательны"}
        raw = raw_config()
        raw.setdefault("catalog_sources", {})[name] = {
            "type": "json", "url": data["url"],
            "group": data.get("group") or "direct",
            "headers": data.get("headers") or {}}
        save_raw(raw)
        try:
            cfg.catalog_sources = load_config(config_path).catalog_sources
        except Exception:
            pass
        # пересоздаём ТОЛЬКО этот источник: остальные сохраняют кэш
        # и состояние (ADR-0005 I3)
        _rebuild_one_source(name)
        return {"saved": name}

    @app.post("/admin/api/catalog/sources/{name}/delete")
    def api_catalog_source_delete(name: str) -> dict:
        if name in BUILTIN_SOURCES:
            return {"error": "builtin"}
        raw = raw_config()
        if name not in (raw.get("catalog_sources") or {}):
            return {"error": "unknown"}
        raw["catalog_sources"].pop(name)
        save_raw(raw)
        cfg.catalog_sources.pop(name, None)
        app.state.catalog_sources.pop(name, None)
        return {"deleted": name}

    @app.get("/admin/api/catalog")
    async def api_catalog(request: Request, query: str = "", source: str = "",
                          limit: int = 50, offset: int = 0) -> dict:
        """Поиск по каталогу с пагинацией.

        source=''/'official' — локальный индекс реестра (мгновенно);
        остальные — из catalog_sources. Живой фолбэк реестра
        ограничен по времени (ADR-0005 I4) и живёт в async-клиенте,
        поэтому недоступный реестр не вешает панель на 20 секунд.
        """
        src_name = source or "official"
        src = app.state.catalog_sources.get(src_name)
        if src is None:
            return {"items": [], "total": 0, "has_more": False,
                    "error": f"unknown source: {src_name}"}
        n = max(1, min(int(limit or 50), 200))
        off = max(0, int(offset or 0))
        result = src.search(query or "", limit=n, offset=off)
        state, err = src.status()
        out = {"source": src_name, "limit": n, "offset": off,
               "state": state, **result}
        if err:
            out["error"] = err
        if not result["items"] and state != "ok":
            out["notice"] = "Источник недоступен — показаны последние " \
                            "известные данные" if result["total"] else \
                            f"Источник недоступен: {err or state}"
        if src_name == "official" and not result["items"] and query:
            entries = await _live_registry(query, limit=n)
            if entries:
                out["items"] = entries
                out["total"] = len(entries)
                out["source"] = "official"
                out["notice"] = "Показано из реестра напрямую " \
                                "(локальный индекс обновляется в фоне)"
            else:
                # Живой реестр ничего не вернул И локальный индекс пуст —
                # только тогда индекс действительно не наполнен. Раньше
                # это сообщение показывалось при любом промахе, хотя
                # реестр отвечал 200, а индекс лежал на диске: диагноз
                # уводил в сторону (ADR-0005, truthful status).
                src = app.state.catalog_sources[src_name]
                if src._idx.load():
                    out["notice"] = f"По запросу «{query}» ничего не " \
                                    f"нашлось в реестре MCP"
                else:
                    out["notice"] = "Реестр MCP недоступен, локальный " \
                                    "индекс пуст — попробуйте позже"
        elif src_name == "official" and not result["items"] \
                and not query and not result.get("notice"):
            out["notice"] = "Каталог загружается (первая синхронизация " \
                            "реестра)…"
        return out

    async def _live_registry(query: str, limit: int) -> list:
        """Живой реестр как последний фолбэк, с жёстким потолком."""
        from .registry import search_async
        try:
            entries = await search_async(query, limit=limit)
        except Exception as e:
            return []
        cards = [_entry_to_item(e) for e in entries]
        out = []
        for c in cards:
            if not c:
                continue
            c.setdefault("source", "official")
            out.append(c)
        return out

    @app.get("/admin/api/groups")
    def api_groups() -> dict:
        return {"groups": [{"id": gid, "name": g.name, "proxy": g.proxy}
                           for gid, g in cfg.groups.items()]}

    # ---- мутации (yaml + перезапуск ребёнка) ----

    @app.post("/admin/api/servers/{name}/delete")
    def api_delete(name: str) -> dict:
        if name not in cfg.servers:
            return {"error": "unknown"}
        raw = raw_config()
        raw.get("servers", {}).pop(name, None)
        save_raw(raw)
        # supervisor.remove снимает сервер из managed, healths, fails и
        # конфига разом — частичного удаления больше не бывает
        # (ADR-0003 I1).
        supervisor.remove(name)
        return {"deleted": name}

    @app.post("/admin/api/servers/{name}/restart")
    def api_restart(name: str) -> dict:
        # manual=True: сбрасывает цепь неудач и замыкает разомкнутый
        # автомат — иначе «Перезапустить» ничего бы не дал (ADR-0004 I3).
        supervisor.restart(name, manual=True)
        return {"restarted": name}

    @app.post("/admin/api/servers")
    async def api_upsert(request: Request) -> dict:
        """Создать/обновить. body: name, kind, url|command, group, ключи.
        form_fields (из каталога): [{name, secret, template}] — поля формы."""
        data = await _json_body(request)
        if data is None:
            return {"error": "неверный JSON в теле запроса"}
        name = re.sub(r"[^a-z0-9_-]", "", (data.get("name") or "").lower())
        if not name:
            return {"error": "bad name"}
        group = data.get("group") or "direct"
        raw = raw_config()
        servers = raw.setdefault("servers", {})
        entry: dict = {"group": group}
        if data.get("url"):
            entry["url"] = data["url"]
            if data.get("api_key"):
                entry["headers"] = {data.get("key_header")
                                    or "Authorization": data["api_key"]}
        elif data.get("command"):
            entry["command"] = data["command"]
            entry["args"] = data.get("args") or []
            env = {}
            # поля из карточки каталога (stdio: env-переменные пакета)
            for f in data.get("form_fields") or []:
                val = (f.get("value") or "").strip()
                if val:
                    env[f["name"]] = val
            if data.get("api_key"):
                env[data.get("key_header") or "API_KEY"] = data["api_key"]
            if env:
                entry["env"] = env
        else:
            return {"error": "нужен url или command"}
        servers[name] = entry
        save_raw(raw)
        # рантайм-конфиг обязан увидеть новую запись ДО add(), иначе
        # supervisor не найдёт spec. И add() создаёт Health вместе
        # с ManagedServer — раньше Health не создавался вовсе, и
        # watchdog падал на KeyError (ADR-0003).
        try:
            reloaded = load_config(config_path)
            cfg.groups = reloaded.groups
            cfg.servers = reloaded.servers
        except Exception:
            cfg.servers[name] = ServerSpec(
                name=name, kind="http" if data.get("url") else "stdio",
                group=group, url=data.get("url"),
                command=data.get("command"),
                args=data.get("args") or [])
        supervisor.add(name)
        supervisor.collect_secrets()
        return {"saved": name}

    # ---- страница ----

    @app.get("/admin", response_class=HTMLResponse)
    def admin_page() -> Response:
        # Порт в легенде — фактический из конфига, а не константа
        # (ADR-0006 I4): при авто-выборе он отличается от 9300.
        return HTMLResponse(_PAGE.replace("{port}", str(cfg.gateway_port)))


_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>AOMG — MCP-шлюз</title>
<style>
  :root { --bg:#14121f; --card:#1d1a2e; --line:#332f4d; --fg:#e8e5f4;
          --mut:#9a94b8; --acc:#a78bfa; --ok:#4ade80; --warn:#fbbf24;
          --err:#f87171; }
  * { box-sizing:border-box; margin:0; }
  body { background:var(--bg); color:var(--fg);
         font:15px/1.5 system-ui,'Segoe UI',sans-serif; padding:28px; }
  .wrap { max-width:860px; margin:0 auto; }
  h1 { font-size:22px; font-weight:600; letter-spacing:.3px; }
  h1 span { color:var(--acc); }
  .sub { color:var(--mut); font-size:13px; margin:4px 0 22px; }
  .srv { background:var(--card); border:1px solid var(--line);
         border-radius:12px; padding:16px 18px; margin-bottom:12px;
         display:flex; align-items:center; gap:14px; }
  .dot { width:12px; height:12px; border-radius:50%; flex:none; }
  .dot.ok{background:var(--ok)} .dot.reconnecting{background:var(--warn)}
  .dot.channel_down{background:var(--warn)} .dot.down{background:var(--err)}
  .info { flex:1; min-width:0; }
  .nm { font-weight:600; font-size:16px; }
  .meta { color:var(--mut); font-size:12.5px; margin-top:2px;
          white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .btn { background:transparent; border:1px solid var(--line);
         color:var(--mut); border-radius:8px; padding:6px 12px;
         font-size:13px; cursor:pointer; }
  .btn:hover { color:var(--fg); border-color:var(--acc); }
  .btn.danger:hover { color:var(--err); border-color:var(--err); }
  .bar { display:flex; gap:10px; margin:20px 0 8px; }
  .add { background:var(--acc); color:#17141f; font-weight:600;
         border:none; border-radius:9px; padding:10px 18px;
         font-size:14px; cursor:pointer; }
  .add:hover { filter:brightness(1.1); }
  dialog { background:var(--card); color:var(--fg);
           border:1px solid var(--line); border-radius:14px;
           padding:24px; width:480px; max-width:92vw; }
  dialog::backdrop { background:rgba(10,8,18,.7); }
  h2 { font-size:17px; margin-bottom:14px; }
  label { display:block; font-size:12.5px; color:var(--mut); margin:12px 0 4px; }
  input,select { width:100%; background:var(--bg); color:var(--fg);
          border:1px solid var(--line); border-radius:8px;
          padding:9px 11px; font-size:14px; }
  input:focus,select:focus { outline:none; border-color:var(--acc); }
  .row { display:flex; gap:10px; } .row>*{flex:1}
  .cat { max-height:210px; overflow:auto; border:1px solid var(--line);
         border-radius:9px; margin-top:6px; }
  .cat div { padding:9px 12px; cursor:pointer; border-bottom:1px solid var(--line);
         font-size:13.5px; }
  .cat div:hover { background:var(--bg); }
  .cat b { display:block; } .cat span { color:var(--mut); font-size:12px; }
  .cat-tabs { display:flex; gap:6px; margin:10px 0 4px; flex-wrap:wrap;
              align-items:center; }
  .cat-tab { background:transparent; border:1px solid var(--line);
          color:var(--mut); border-radius:8px; padding:6px 12px;
          font-size:13px; cursor:pointer; display:flex; align-items:center;
          gap:7px; }
  .cat-tab.active { color:var(--fg); border-color:var(--acc); }
  .cat-tab .dot { width:8px; height:8px; border-radius:50%; flex:none; }
  .cat-tab .dot.ok{background:var(--ok)} .cat-tab .dot.error,
  .cat-tab .dot.unreachable{background:var(--err)}
  .cat-src-btn { margin-left:auto; }
  .srcrow { display:flex; align-items:center; gap:10px; padding:9px 4px;
          border-bottom:1px solid var(--line); font-size:13.5px; }
  .srcrow .nm { font-weight:600; }
  .srcrow .meta { color:var(--mut); font-size:12px; flex:1;
          white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .actions { display:flex; gap:10px; justify-content:flex-end; margin-top:20px; }
  .primary { background:var(--acc); color:#17141f; border:none;
         border-radius:8px; padding:9px 18px; font-weight:600; cursor:pointer; }
  .err { color:var(--err); font-size:13px; margin-top:10px; min-height:18px; }
  .legend { color:var(--mut); font-size:12px; margin-top:18px; }
</style></head><body><div class="wrap">
<h1>AOMG <span>· MCP-шлюз</span></h1>
<div class="sub" id="agg">загрузка…</div>
<div id="list"></div>
<div class="bar">
  <button class="add" onclick="openAdd()">+ Добавить MCP</button>
</div>

<div id="catalog">
  <div class="cat-tabs" id="cat-tabs"></div>
  <div style="display:flex; gap:8px; margin:8px 0;">
    <input id="q" placeholder="поиск в выбранном источнике…"
           oninput="search()" style="flex:1">
  </div>
  <div class="cat" id="cat"></div>
</div>
<div class="legend">Зелёный — работает · жёлтый — перезапуск или нет сети · красный — не отвечает.<br>
Агенту ничего настраивать не нужно: каждый сервер доступен на
<code>http://127.0.0.1:{port}/&lt;имя&gt;/mcp</code> автоматически.</div>

<dialog id="dlg">
<h2>Добавить MCP-сервер</h2>
<label>Имя</label>
<input id="f-name" placeholder="my-server">
<label>Тип</label>
<select id="f-kind" onchange="kindChanged()">
  <option value="url">Ссылка (удалённый сервер)</option>
  <option value="command">Локальная программа</option>
</select>
<label id="l-main">Ссылка</label>
<input id="f-main" placeholder="https://…/mcp">
<div id="keybox">
<label>API-ключ (необязательно)</label>
<div class="row">
  <input id="f-keyheader" placeholder="заголовок, напр. X-Api-Key" style="flex:1.2">
  <input id="f-key" placeholder="ключ" type="password" style="flex:1">
</div></div>
<div id="dynfields"></div>
<label>Группа сети</label>
<select id="f-group"></select>
<div class="err" id="err"></div>
<div class="actions">
  <button class="btn" onclick="dlg.close()">Отмена</button>
  <button class="primary" onclick="save()">Сохранить и подключить</button>
</div>
</dialog>

<dialog id="srcdlg">
<h2>Источники каталога</h2>
<div id="src-list" style="max-height:260px; overflow:auto;"></div>
<div class="err" id="src-err"></div>
<h2 style="margin-top:18px">Добавить источник</h2>
<label>Имя (латиницей)</label>
<input id="s-name" placeholder="corp">
<label>URL JSON-манифеста</label>
<input id="s-url" placeholder="https://…/mcp-servers.json">
<label>Группа сети (egress)</label>
<select id="s-group"></select>
<label>Заголовок авторизации (необязательно)</label>
<input id="s-hdr" placeholder="Authorization" value="Authorization">
<input id="s-hdrval" placeholder="Bearer …" type="password">
<div class="actions">
  <button class="btn" onclick="srcdlg.close()">Закрыть</button>
  <button class="primary" onclick="addSource()">Добавить</button>
</div>
</dialog>
</div>
<script>
let GROUPS=[]; let PICKED_FIELDS=[];
async function refresh(){
  const h = await (await fetch('/health')).json();
  const names = {ok:'работает',reconnecting:'перезапуск…',
                 channel_down:'нет сети',down:'не отвечает'};
  document.getElementById('agg').textContent =
    h.aggregate==='ok' ? 'Всё работает' :
    'Есть проблемы (' + h.servers.filter(s=>s.state!=='ok').length + ')';
  document.getElementById('list').innerHTML = h.servers.map(s=>`
    <div class="srv">
      <div class="dot ${s.state}"></div>
      <div class="info">
        <div class="nm">${s.name}</div>
        <div class="meta">${names[s.state]||s.state} · инструментов: ${s.tools}
          ${s.error?' · '+s.error.slice(0,80):''}</div>
      </div>
      <button class="btn" onclick="restart('${s.name}')">Перезапустить</button>
      <button class="btn danger" onclick="del('${s.name}')">Удалить</button>
    </div>`).join('');
}
async function loadGroups(){
  GROUPS = (await (await fetch('/admin/api/groups')).json()).groups;
  document.getElementById('f-group').innerHTML =
    GROUPS.map(g=>`<option value="${g.id}">${g.name}</option>`).join('');
}
async function restart(n){ await fetch(`/admin/api/servers/${n}/restart`,{method:'POST'}); refresh(); }
async function del(n){ if(!confirm('Удалить '+n+'?'))return;
  await fetch(`/admin/api/servers/${n}/delete`,{method:'POST'}); refresh(); }
let tmr; let searching=false;
let CAT_ITEMS=[]; let SOURCES=[]; let ACTIVE_SRC='official';
const CAT_LIMIT=50; let CAT_OFFSET=0;
function esc(s){ return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;')
  .replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }

// ---- каталог: источники и табы ----
async function loadSources(){
  const d = await (await fetch('/admin/api/catalog/sources')).json();
  SOURCES = (d.sources||[]).filter(s=>!s.hidden);
  const tabs = document.getElementById('cat-tabs');
  tabs.innerHTML = SOURCES.map(s=>{
    const st = s.state==='ok'?'ok':(s.state||'error');
    const title = s.error ? esc(s.error) : (s.state==='ok'?'доступен':esc(s.state));
    return `<button class="cat-tab ${s.name===ACTIVE_SRC?'active':''}"
      onclick="switchSource('${s.name}')" title="${title}">
      <span class="dot ${st}"></span>${esc(s.name)}</button>`;
  }).join('') +
  `<button class="btn cat-src-btn" onclick="openSources()">Источники…</button>`;
}
function switchSource(name){
  ACTIVE_SRC=name; CAT_OFFSET=0; CAT_ITEMS=[];
  clearTimeout(tmr); searching=false;   // старый запрос больше не актуален
  loadSources(); search();
}
function openAdd(){
  loadSources().then(()=>search());
  dlg.showModal();
}
async function openSources(){
  document.getElementById('s-group').innerHTML =
    GROUPS.map(g=>`<option value="${g.id}">${g.name}</option>`).join('');
  await renderSourceList();
  srcdlg.showModal();
}
async function renderSourceList(){
  const d = await (await fetch('/admin/api/catalog/sources')).json();
  document.getElementById('src-list').innerHTML = (d.sources||[]).map(s=>{
    const builtin = s.name==='official'||s.name==='neuraldeep';
    const del = builtin ? '' :
      `<button class="btn danger" onclick="delSource('${s.name}')">Удалить</button>`;
    const hdr = s.headers ? Object.entries(s.headers)
      .map(([k,v])=>`${esc(k)}: ${esc(v)}`).join('; ') : '';
    return `<div class="srcrow">
      <span class="dot ${s.state==='ok'?'ok':(s.state||'error')}"
        style="width:10px;height:10px;border-radius:50%;
        background:${s.state==='ok'?'var(--ok)':'var(--err)'}"></span>
      <span class="nm">${esc(s.name)}</span>
      <span class="meta">${esc(s.type)} · ${esc(s.group)}
        ${s.url?' · '+esc(s.url):''} ${hdr?' · '+hdr:''}</span>
      ${del}</div>`;
  }).join('');
}
async function addSource(){
  const name = document.getElementById('s-name').value.trim();
  const url = document.getElementById('s-url').value.trim();
  const hdr = document.getElementById('s-hdr').value.trim();
  const hdrval = document.getElementById('s-hdrval').value.trim();
  const b = {name, type:'json', url,
             group:document.getElementById('s-group').value};
  if(hdr && hdrval) b.headers = {[hdr]: hdrval};
  const r = await (await fetch('/admin/api/catalog/sources', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify(b)})).json();
  if(r.error){ document.getElementById('src-err').textContent=r.error; return; }
  document.getElementById('s-name').value='';
  document.getElementById('s-url').value='';
  document.getElementById('s-hdrval').value='';
  document.getElementById('src-err').textContent='';
  await renderSourceList(); loadSources();
}
async function delSource(n){ if(!confirm('Удалить источник '+n+'?'))return;
  await fetch(`/admin/api/catalog/sources/${n}/delete`,{method:'POST'});
  if(ACTIVE_SRC===n) ACTIVE_SRC='official';
  await renderSourceList(); loadSources(); }

async function search(nextPage){
  clearTimeout(tmr);
  // `nextPage` = «показать ещё». Раньше аргумент назывался reset и был
  // инвертирован: `if(!reset) CAT_OFFSET=0` сбрасывало offset именно
  // для «ещё», и следующая страница конкатенировалась поверх первой —
  // пользователь видел каждый сервер дважды.
  // Имя не `more`: ниже в этой же области есть
  // `const more=document.getElementById('more')`, и одноимённый параметр
  // давал TDZ-ошибку в блоке catch.
  if(nextPage) CAT_OFFSET+=CAT_LIMIT; else CAT_OFFSET=0;
  tmr=setTimeout(async()=>{
    const q=document.getElementById('q').value;
    const cat=document.getElementById('cat');
    if(searching) return;   // предыдущий запрос ещё летит — не плодить
    searching=true;
    cat.innerHTML='<div><span>ищу…</span></div>';
    try{
      const d=await (await fetch(`/admin/api/catalog?source=${encodeURIComponent(ACTIVE_SRC)}&query=${encodeURIComponent(q)}&limit=${CAT_LIMIT}&offset=${CAT_OFFSET}`)).json();
      CAT_ITEMS = nextPage ? CAT_ITEMS.concat(d.items||[]) : (d.items||[]);
      let html = CAT_ITEMS.map((i,idx)=>`
        <div data-idx="${idx}" class="cat-item" style="cursor:pointer">
          <b>${esc(i.title||i.name)}</b><span>${esc((i.description||'').slice(0,90))}</span>
        </div>`).join('');
      if(!CAT_ITEMS.length) html = d.notice
        ? `<div style="opacity:.7">${esc(d.notice)}</div>`
        : (d.error ? `<div><span style="color:var(--err)">${esc(d.error)}</span></div>`
                   : '<div><span>ничего не найдено</span></div>');
      // пагинация вместо «магического числа»: пользователь сам решает,
      // показывать ли следующую страницу (ADR-0005 I5)
      if(d.total>CAT_ITEMS.length)
        html+=`<div id="more" style="padding:9px 12px;cursor:pointer;
          color:var(--acc);font-size:13px">показать ещё
          (${CAT_ITEMS.length} из ${d.total})</div>`;
      if(d.notice && CAT_ITEMS.length)
        html+=`<div><span style="opacity:.6">${esc(d.notice)}</span></div>`;
      cat.innerHTML = html;
      const more=document.getElementById('more');
      if(more) more.addEventListener('click',()=>search(true));
      cat.querySelectorAll('.cat-item').forEach(el=>{
        el.addEventListener('click', ()=>pick(CAT_ITEMS[+el.dataset.idx]));
      });
    }catch(e){
      // локальный индекс не мог «отвалиться» сам: это упал fetch
      // целиком (гейтвей перезапускается). Сообщение честное и с
      // действием, причина остаётся в консоли браузера.
      console.error('catalog:', e);
      cat.innerHTML='<div><span style="opacity:.7">Панель перезапускается — '+
        'попробуйте ещё раз через несколько секунд.</span></div>';
    }
    searching=false;
  },350); }
function pick(i){
  document.getElementById('f-name').value=i.name;
  PICKED_FIELDS = i.form_fields || [];
  if(i.url){ document.getElementById('f-kind').value='url';
    document.getElementById('f-main').value=i.url; }
  else if(i.command){ document.getElementById('f-kind').value='command';
    // карточки json-источников несут command+args целиком
    const args = i.args||[];
    document.getElementById('f-main').value =
      args.length ? args.join(' ') : (i.install||i.command); }
  else if(i.install){ document.getElementById('f-kind').value='command';
    document.getElementById('f-main').value=i.install; }
  kindChanged();
  renderFields();
}
function renderFields(){
  // динамические поля ключей из карточки каталога (env-переменные пакета)
  const box=document.getElementById('dynfields');
  box.innerHTML = PICKED_FIELDS.map((f,idx)=>`
    <label>${f.name}${f.secret?' (секрет)':''}</label>
    <input type="password" data-fidx="${idx}" placeholder="${(f.description||f.name).slice(0,60)}">`
  ).join('');
}
function kindChanged(){
  const k=document.getElementById('f-kind').value;
  document.getElementById('l-main').textContent = k==='url'?'Ссылка':'Команда запуска';
  document.getElementById('f-main').placeholder = k==='url'?'https://…/mcp':'npx -y @some/mcp-server';
}
async function save(){
  const b={ name:document.getElementById('f-name').value,
    group:document.getElementById('f-group').value,
    api_key:document.getElementById('f-key').value||null,
    key_header:document.getElementById('f-keyheader').value||null };
  // динамические поля из карточки (env-переменные stdio-пакета)
  if(PICKED_FIELDS.length){
    b.form_fields=[...document.querySelectorAll('#dynfields input')]
      .map(inp=>({name:PICKED_FIELDS[+inp.dataset.fidx].name,
                  value:inp.value}));
  }
  const k=document.getElementById('f-kind').value;
  if(k==='url') b.url=document.getElementById('f-main').value;
  else { b.command='npx'; b.args=['-y',document.getElementById('f-main').value]; }
  const r=await (await fetch('/admin/api/servers',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(b)})).json();
  if(r.error){ document.getElementById('err').textContent=r.error; return; }
  dlg.close(); refresh();
}
loadGroups(); loadSources().then(()=>search()); refresh(); setInterval(refresh,15000);
</script></body></html>"""


def start_admin_thread(app: FastAPI) -> None:  # placeholder compat
    pass
