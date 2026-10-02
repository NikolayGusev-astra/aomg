"""Исполняемый тест разметки списка серверов (реальный JS из панели).

Список из 16 серверов выглядел как случайная последовательность строк
в порядке конфига: упавший сервер тонул среди зелёных, а причина
падения была обрезана до 80 символов прямо в meta-строке. Проверяем
фактический код refresh() из aomg/admin.py в Node с DOM-заглушками.
"""
import json
import pathlib
import re
import subprocess

ADMIN = pathlib.Path(__file__).resolve().parents[1] / "aomg" / "admin.py"
NODE = "node"


def extract_function(src: str, name: str) -> str:
    m = re.search(rf"async function {name}\(\)\{{", src)
    assert m, f"не найдена функция {name}"
    i = src.index("{", m.start())
    depth, j = 0, i
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                # сигнатуру берём из найденного совпадения, а не срезаем
                # с фигурной скобки - иначе теряется имя функции
                return src[m.start():j + 1]
        j += 1
    raise AssertionError("несбалансированные скобки")


def run(js: str, payload: dict) -> tuple:
    """Возвращает (текст шапки, html списка)."""
    # Собираем строку без f-string/.format: внутри много фигурных
    # скобок (DOM-заглушки), и двойная подстановка ломает харнесс.
    harness = """
const _els = {};
const el = id => (_els[id] = _els[id] || {
  _id:id, _html:'', textContent:'',
  set innerHTML(v){ this._html = v; }, get innerHTML(){ return this._html; }
});
const document = { getElementById: id => el(id) };
const fetch = async (u) => ({ json: async () => (__PAYLOAD__) });
__JS__
// esc в панели объявлена через function (hoisted), поэтому её нельзя
// воспроизводить как const - иначе получим TDZ, которого в бою нет
__ESC__
// refresh() асинхронна: innerHTML заполняется после await, поэтому
// читать его сразу после вызова - читать пустую строку
(async () => {
  await refresh();
  // шапка и список живут в разных узлах: их нельзя проверять одним
  // куском разметки. Перевод строки - \\n внутри JS-литерала, иначе
  // node получает сырую строку и падает на синтаксисе.
  process.stdout.write('AGG:' + document.getElementById('agg').textContent
    + '\\nLIST:' + document.getElementById('list').innerHTML);
})();
"""
    harness = harness.replace("__PAYLOAD__", json.dumps(payload))
    harness = harness.replace("__JS__", js)
    harness = harness.replace("__ESC__", esc_js())
    r = subprocess.run([NODE, "-e", harness], capture_output=True,
                       text=True, errors="replace", timeout=90)
    assert r.returncode == 0, f"node упал: {r.stderr[:400]}"
    out = r.stdout
    assert out.startswith("AGG:"), f"не разобрали вывод: {out[:120]}"
    agg, _, body = out[4:].partition("\nLIST:")
    return agg, body


def agg_of(js: str, payload: dict) -> str:
    return run(js, payload)[0]


def refresh_js() -> str:
    src = ADMIN.read_text(encoding="utf-8")
    return extract_function(src, "refresh")


def esc_js() -> str:
    """Реальный esc() из панели.

    Смысл: харнесс не должен переписывать экранирование своими словами -
    тогда тест проверит подмену, а не панель. TDZ-ловушка, кстати,
    повторяет баг, который уже случался в каталоге.
    """
    src = ADMIN.read_text(encoding="utf-8")
    m = re.search(r"function esc\(s\)\{(.*?)\}\n", src, re.S)
    assert m, "не найдена функция esc в панели"
    return "function esc(s){" + m.group(1) + "}"


def health(**kw) -> dict:
    h = {"aggregate": "ok", "servers": []}
    h["servers"].extend(kw.pop("servers", []))
    h.update(kw)
    return h


def srv(name, state="ok", tools=0, error=None):
    return {"name": name, "state": state, "tools": tools, "error": error}


def test_failed_server_goes_first():
    """Упавший сервер обязан быть первым, а не теряться среди зелёных."""
    js = refresh_js()
    _, html = run(js, health(aggregate="channel_down", servers=[
        srv("ontonet", tools=67), srv("jira", "channel_down", 0,
                                    "McpError: Connection closed"),
        srv("mattermost", tools=53),
    ]))
    assert "Требует внимания" in html, "нет секции проблем"
    assert html.index("jira") < html.index("ontonet"), \
        "упавший сервер не выше рабочих"


def test_reason_is_shown_not_truncated():
    """Причина падения показывается целиком, а не 80 символами в meta."""
    js = refresh_js()
    long = "ImportError: cannot import name 'Icon' from 'mcp.types' " \
           "C:\\\\path\\\\to\\\\astra_jira_dc_mcp\\\\server.py line 35"
    _, html = run(js, health(aggregate="channel_down",
                             servers=[srv("jira", "channel_down", 0, long)]))
    assert "ImportError" in html, "причина не показана"
    assert "mcp.types" in html, "хвост причины обрезан"


def test_error_is_escaped():
    """Текст ошибки не должен ломать разметку через < > &."""
    js = refresh_js()
    _, html = run(js, health(aggregate="down",
                             servers=[srv("x", "down", 0, "<script>alert(1)</script>")]))
    assert "<script>alert" not in html, "XSS: текст ошибки не экранирован"
    assert "&lt;script&gt;" in html, "экранирование не сработало"


def test_counts_shown_for_ok_only():
    """Число тулов показывается у рабочих; у упавшего - прочерк."""
    js = refresh_js()
    _, html = run(js, health(aggregate="channel_down", servers=[
        srv("ontonet", tools=67), srv("jira", "down", 0, "boom")]))
    ok_block = html.split("Работают")[1]
    assert "67 тулов" in ok_block, "число тулов не показано"
    bad_block = html.split("Требует внимания")[1].split("Работают")[0]
    assert "—</div>" in bad_block, "у упавшего показано число тулов"


def test_all_ok_has_no_problem_section():
    """Когда всё работает, лишней секции быть не должно."""
    js = refresh_js()
    _, html = run(js, health(aggregate="ok", servers=[
        srv("a", tools=1), srv("b", tools=2)]))
    assert "Требует внимания" not in html
    assert "Работают" in html


def test_aggregate_counts_problems():
    """Шапка показывает число проблем, а не считает по конфигу."""
    js = refresh_js()
    agg = agg_of(js, health(aggregate="channel_down", servers=[
        srv("a", "down", 0, "x"), srv("b", "down", 0, "y"),
        srv("c", tools=1)]))
    assert agg == "Есть проблемы (2)"
