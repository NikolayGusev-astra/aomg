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


def _mask_headers(hdrs: dict) -> dict:
    """Секреты в заголовках маскируются: по имени ключа (key/token/…) и
    по классическим носителям (Authorization, Cookie)."""
    return {k: ("***" if v and (_is_secret(k)
                                or k.lower() in _SECRET_KEY_NAMES) else v)
            for k, v in hdrs.items()}


def register_admin(app: FastAPI, cfg: Config, supervisor: Supervisor,
                   healths: dict[str, Health], config_path: Path,
                   restart_watchdog: callable) -> None:
    """Роуты админки. config_path нужен для сохранения yaml."""

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
        return {"name": name, "kind": spec.kind, "group": spec.group,
                "url": spec.url, "command": spec.command, "env": env,
                "headers": hdrs, "state": healths[name].state,
                "tools": healths[name].tools, "error": healths[name].error}

    # ---- данные ----

    @app.get("/admin/api/servers")
    def api_servers() -> dict:
        return {"servers": [spec_to_public(n, s)
                            for n, s in cfg.servers.items()]}

    # ---- каталог: источники (ADR-0002) ----

    def _build_catalog_sources(cfg_obj: Config) -> dict:
        """official + neuraldeep (встроенные) + json-источники из конфига."""
        from .json_source import JsonSource
        sources: dict = {"official": _OfficialIndexAdapter(
            config_path.parent / "registry-index.json")}
        nd_spec = cfg_obj.catalog_sources.get("neuraldeep")
        if not (nd_spec and nd_spec.hidden):
            from .catalog import NeuralDeepSource
            sources["neuraldeep"] = NeuralDeepSource()
        for name, spec in cfg_obj.catalog_sources.items():
            if spec.type == "json":
                sources[name] = JsonSource(spec, cache_dir=config_path.parent)
        return sources

    class _OfficialIndexAdapter:
        """Тонкая обёртка CatalogIndex под интерфейс источников каталога."""

        name = "official"

        def __init__(self, index_path: Path):
            from .index import CatalogIndex
            self._idx = CatalogIndex(index_path)

        def state_info(self) -> dict:
            age = self._idx.age()
            if age is None:
                return {"state": "error", "error": "index not built yet"}
            return {"state": "ok", "error": None}

        def search(self, query: str, limit: int = 20) -> list[dict]:
            stale = self._idx.is_stale()
            items = self._idx.search(query, limit=limit)
            if not items:
                try:
                    entries = registry_search(query, limit=limit)
                    items = [c for c in
                             (_entry_to_item(e) for e in entries) if c]
                except Exception:
                    pass
            elif stale:
                self._idx.ensure_fresh()
            for card in items:
                card.setdefault("source", "official")
            return items

    app.state.catalog_sources = _build_catalog_sources(cfg)
    BUILTIN_SOURCES = ("official", "neuraldeep")

    def _source_public(name: str, src) -> dict:
        if name == "official":
            info = src.state_info()
        elif hasattr(src, "state"):          # JsonSource
            info = {"state": src.state, "error": src.error}
        else:                                 # NeuralDeepSource — ленивый
            info = {"state": "ok", "error": None}
        spec = cfg.catalog_sources.get(name)
        out = {"name": name,
               "type": spec.type if spec else
                       ("json" if name == "neuraldeep" else "builtin"),
               "group": spec.group if spec else "direct",
               "hidden": spec.hidden if spec else False,
               **info}
        if spec and spec.url:
            out["url"] = spec.url
        if spec and spec.headers:
            out["headers"] = _mask_headers(spec.headers)
        return out

    @app.get("/admin/api/catalog/sources")
    def api_catalog_sources() -> dict:
        # official всегда первый
        srcs = app.state.catalog_sources
        names = [n for n in ("official", "neuraldeep") if n in srcs] + \
                [n for n in srcs if n not in BUILTIN_SOURCES]
        return {"sources": [_source_public(n, srcs[n]) for n in names]}

    @app.post("/admin/api/catalog/sources")
    async def api_catalog_source_upsert(request: Request) -> dict:
        data = await request.json()
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
            reloaded = load_config(config_path)
            cfg.catalog_sources = reloaded.catalog_sources
        except Exception:
            pass
        app.state.catalog_sources = _build_catalog_sources(cfg)
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
    def api_catalog(query: str = "", source: str = "") -> dict:
        """Поиск по каталогу. source='' или 'official' — локальный индекс
        реестра (aomg/index.py, мгновенный, stale-while-revalidate);
        другой source — соответствующий источник из catalog_sources."""
        from . import index as _index

        src_name = source or "official"
        src = app.state.catalog_sources.get(src_name)
        if src is None:
            return {"items": [], "error": f"unknown source: {src_name}"}

        if src_name != "official":
            items = src.search(query or "", limit=20)
            out: dict = {"items": items, "source": src_name}
            if getattr(src, "state", "ok") != "ok":
                out["error"] = getattr(src, "error", None) or src.state
                out["items"] = []
            return out

        idx: _index.CatalogIndex = getattr(api_catalog, "_index", None)
        if idx is None:
            idx = api_catalog._index = _index.CatalogIndex(
                config_path.parent / "registry-index.json")

        q = query or "mcp"
        stale = idx.is_stale()
        items = idx.search(q, limit=20)
        if items:
            if stale:
                idx.ensure_fresh()  # фоново обновит, ответ не ждём
            return {"items": items, "cached": stale, "source": "official"}
        # индекс пуст (первый запуск и sync ещё не дошёл) — живой фолбэк
        try:
            entries = registry_search(q, limit=20)
        except Exception as ex:
            return {"items": [], "error": str(ex)[:150], "source": "official"}
        items = [_entry_to_item(e) for e in entries if _entry_to_item(e)]
        if items:
            try:
                idx.ensure_fresh()  # дольём полный индекс в фоне
            except Exception:
                pass
            return {"items": items, "cached": False, "source": "official"}
        # пусто и в реестре: возможно, индекс протух — обновим в фоне
        if stale:
            idx.ensure_fresh()
        return {"items": [], "cached": False, "source": "official"}

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
        supervisor.remove(name)
        healths.pop(name, None)
        return {"deleted": name}

    @app.post("/admin/api/servers/{name}/restart")
    def api_restart(name: str) -> dict:
        supervisor.restart(name)
        return {"restarted": name}

    @app.post("/admin/api/servers")
    async def api_upsert(request: Request) -> dict:
        """Создать/обновить. body: name, kind, url|command, group, ключи.
        form_fields (из каталога): [{name, secret, template}] — поля формы."""
        data = await request.json()
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
        # рантайм-конфиг обязан увидеть новую запись ДО ensure(), иначе
        # supervisor молча пропустит спавн (баг: запись в yaml была,
        # а в списке сервер не появлялся до полного рестарта AOMG)
        try:
            reloaded = load_config(config_path)
            cfg.groups = reloaded.groups
            cfg.servers = reloaded.servers
        except Exception:
            pass  # битый yaml не роняем — ensure() просто не найдёт запись
        supervisor.ensure(name)
        return {"saved": name}

    # ---- страница ----

    @app.get("/admin", response_class=HTMLResponse)
    def admin_page() -> HTMLResponse:
        return HTMLResponse(_PAGE)


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
  .actions { display:flex; gap:10px; justify-content:flex-end; margin-top:20px; }
  .primary { background:var(--acc); color:#17141f; border:none;
         border-radius:8px; padding:9px 18px; font-weight:600; cursor:pointer; }
  .err { color:var(--err); font-size:13px; margin-top:10px; min-height:18px; }
  .legend { color:var(--mut); font-size:12px; margin-top:18px; }
</style></head><body><div class="wrap">
<h1>AOMG <span>· MCP-шлюз</span></h1>
<div class="sub" id="agg">загрузка…</div>
<div id="list"></div>
<div class="bar"><button class="add" onclick="dlg.showModal()">+ Добавить MCP</button></div>
<div class="legend">Зелёный — работает · жёлтый — перезапуск или нет сети · красный — не отвечает.<br>
Агенту ничего настраивать не нужно: каждый сервер доступен на
<code>http://127.0.0.1:9300/&lt;имя&gt;/mcp</code> автоматически.</div>

<dialog id="dlg">
<h2>Добавить MCP-сервер</h2>
<label>Найти в каталоге</label>
<input id="q" placeholder="например: jira, calendar, github…"
       oninput="search()">
<div class="cat" id="cat"></div>
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
let CAT_ITEMS=[];
function esc(s){ return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;')
  .replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
function search(){ clearTimeout(tmr);
  tmr=setTimeout(async()=>{
    const q=document.getElementById('q').value;
    const cat=document.getElementById('cat');
    if(searching) return;   // предыдущий запрос ещё летит — не плодить
    searching=true;
    cat.innerHTML='<div><span>ищу…</span></div>';
    try{
      const d=await (await fetch(`/admin/api/catalog?query=${encodeURIComponent(q)}`)).json();
      CAT_ITEMS = d.items||[];
      cat.innerHTML = CAT_ITEMS.map((i,idx)=>`
        <div data-idx="${idx}" class="cat-item" style="cursor:pointer">
          <b>${esc(i.title||i.name)}</b><span>${esc((i.description||'').slice(0,90))}</span>
        </div>`).join('') || '<div><span>ничего не найдено</span></div>';
      if(d.error) cat.innerHTML+=`<div><span style="color:var(--err)">${esc(d.error)}</span></div>`;
      if(d.cached) cat.innerHTML+='<div><span style="opacity:.6">из локального индекса (обновляется в фоне)</span></div>';
      cat.querySelectorAll('.cat-item').forEach(el=>{
        el.addEventListener('click', ()=>pick(CAT_ITEMS[+el.dataset.idx]));
      });
    }catch(e){
      cat.innerHTML='<div><span style="color:var(--err)">реестр недоступен</span></div>';
    }
    searching=false;
  },350); }
function pick(i){
  document.getElementById('f-name').value=i.name;
  PICKED_FIELDS = i.form_fields || [];
  if(i.url){ document.getElementById('f-kind').value='url'; }
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
loadGroups(); refresh(); setInterval(refresh,15000);
</script></body></html>"""


def start_admin_thread(app: FastAPI) -> None:  # placeholder compat
    pass
