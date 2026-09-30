"""Пагинация каталога: конкатенация страниц не должна давать дубликаты.

Дефект найден в панели (admin.py, функция search()): аргумент reset был
инвертирован. «Показать ещё» вызывало search(true), а код делал
`if(!reset) CAT_OFFSET=0`, то есть для «ещё» offset сбрасывался в 0, и
вторая страница конкатенировалась поверх первой. Пользователь видел
«Rusender Mcp / Spring Ssh Mcp / Rusender Mcp / Spring Ssh Mcp».

Проверяем саму функцию из отданного HTML, а не переписанную копию:
извлекаем исходник search() и исполняем в node с заглушками DOM/fetch.
"""
import json
import pathlib
import re
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from aomg.admin import register_admin  # noqa: E402
from aomg.config import load_config  # noqa: E402
from aomg.supervisor import Supervisor  # noqa: E402


def _extract_search_js(html: str) -> str:
    """Достать тело функции search() из панели."""
    start = html.index("async function search(")
    depth = 0
    for i in range(start, len(html)):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                return html[start:i + 1]
    raise AssertionError("не нашли конец функции search()")


@pytest.fixture
def panel_js(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "gateway_port: 9413\ngroups:\n  direct: {name: 'D', proxy: null}\n"
        "servers: {}\n", encoding="utf-8")
    app = FastAPI()
    cfg = load_config(cfg_path)
    sup = Supervisor(cfg)
    register_admin(app, cfg, sup, cfg_path, lambda: None)
    return _extract_search_js(TestClient(app).get("/admin").text)


def _harness(panel_js: str, pages: str, calls: str) -> str:
    # обычный f-string не годится: JS полон фигурных скобок,
    # поэтому склеиваем строки
    return (
        # let, а не const: функция search() переприсваивает CAT_ITEMS,
        # и const приводил бы к TypeError внутри try -> тихий catch
        "let CAT_ITEMS=[]; let CAT_OFFSET=0; let CAT_LIMIT=2;\n"
        "let ACTIVE_SRC='official'; let searching=false; let tmr=null;\n"
        "const PAGES=" + pages + ";\n"
        "let page=0;\n"
        "const cat={innerHTML:'', querySelectorAll:()=>[],"
        " addEventListener:()=>{}};\n"
        "function esc(s){return String(s);}\n"
        "const document={getElementById:"
        " (id) => id==='q' ? {value:''} : cat};\n"
        "function fetch(){ return {ok:true, json: async ()=>"
        " PAGES[Math.min(page++, PAGES.length-1)]}; }\n"
        "function pick(){}\n"
        "const sleep=(ms)=>new Promise(r=>setTimeout(r,ms));\n"
        + panel_js + "\n"
        "(async () => {\n" + calls + "\n})();\n"
    )


def _run(panel_js: str, pages, calls: str):
    r = subprocess.run(
        ["node", "-e", _harness(panel_js, json.dumps(pages), calls)],
        capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"node упал: {r.stderr[:500]}"
    return json.loads(r.stdout)


def test_load_more_appends_next_page_without_duplicates(panel_js):
    """«Показать ещё» добавляет страницу, а не повторяет первую."""
    page1 = {"items": [{"name": "a1"}, {"name": "b1"}],
             "total": 4, "has_more": True}
    page2 = {"items": [{"name": "a2"}, {"name": "b2"}],
             "total": 4, "has_more": False}
    names = _run(panel_js, [page1, page2, page2],
                 "search();\nawait sleep(600);\n"
                 "search(true);\nawait sleep(600);\n"
                 "process.stdout.write(JSON.stringify(CAT_ITEMS.map(i=>i.name)));")
    assert names[:4] == ["a1", "b1", "a2", "b2"], \
        f"страницы склеились неверно: {names}"


def test_fresh_search_does_not_keep_previous_results(panel_js):
    """Новый поиск сбрасывает список, а не дописывает к старому."""
    page1 = {"items": [{"name": "x1"}], "total": 1}
    page2 = {"items": [{"name": "y1"}], "total": 1}
    names = _run(panel_js, [page1, page2],
                 "search();\nawait sleep(600);\nsearch();\nawait sleep(600);\n"
                 "process.stdout.write(JSON.stringify(CAT_ITEMS.map(i=>i.name)));")
    assert names == ["y1"], \
        f"повторный поиск должен заменять результаты, а не дописывать: {names}"


def test_offset_advances_between_pages(panel_js):
    """offset обязан расти, иначе вторая страница = первая."""
    assert re.search(r"CAT_OFFSET\s*\+=\s*CAT_LIMIT", panel_js), \
        "offset не увеличивается при пагинации"
    assert re.search(r"CAT_OFFSET\s*=\s*0", panel_js), \
        "нет сброса offset для нового поиска"
