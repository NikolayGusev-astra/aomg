"""NeuralDeep: каталог должен отдавать все карточки, а не две.

Дефект: _HREF искал только /mcp/... и /skills/..., а сайт отдаёт ещё
/skills/<slug> и /cli/<slug>, плюс полные данные лежат в RSC-потоке
Next.js (name/owner/repo/description/type/installs). В итоге панель
показывала 2 карточки вместо всех.

Тесты бьют по реальному формату ответа сайта, зафиксированному здесь как
фикстуры, а не по живому HTTP: регресс должен падать офлайн.
"""
import json
import re
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from aomg.catalog import _HREF, NeuralDeepSource  # noqa: E402

# фрагмент реального /skills: карточка отрисована, но путь /mcp/<slug>
# в HTML есть не у всех — тип и имя приходят из RSC-потока
JSON_LINE = ('[{"name":"1c-log-checker","owner":"acme","repo":'
             '"1c-log-checker","description":"Логи 1С","installs":15,'
             '"trending24h":0,"category":"x","status":"approved",'
             '"type":"skill"},{"name":"yandex-office","owner":"bizyumov",'
             '"repo":"yandex-office","description":"Яндекс Почта и Диск",'
             '"installs":19,"status":"approved","type":"mcp"},'
             '{"name":"coddy-agent","owner":"c","repo":"coddy",'
             '"description":"CLI агент","installs":3,"type":"cli"}]')

# как это выглядит в HTML: RSC-чанк с экранированными кавычками.
# Строка собрана выше, а не вписана с переносами: в raw-строке Python
# висящий обратный слэш НЕ склеивает строки, а попадает в текст и
# ломает последовательность \",
HTML_FIXTURE = (
    '<div class="card-shine">'
    '<a href="/mcp/rusender-mcp">Rusender</a>'
    '<a href="/skill/yandex-direct">Yandex</a>'
    '<a href="/cli/coddy-agent">Coddy</a>'
    '<a href="/skills/validator">Validator</a>'
    '</div><script>self.__next_f.push([1,'
    # separators без пробелов: реальный поток идёт компактным
    # `\"name\":\"...\"`, а не `\"name\": \"...\"` — иначе фикстура
    # проверяла бы формат, которого на сайте нет
    + json.dumps(
        json.dumps([json.loads(x) for x in
                    re.findall(r'\{[^{}]*\}', JSON_LINE)],
                   ensure_ascii=False,      # кириллица как есть, не \uXXXX
                   separators=(",", ":")),   # компактно, без пробелов
        ensure_ascii=False)
    + '])</script>'
)


def test_href_pattern_accepts_all_site_sections():
    """`/skill/` и `/cli/` — реальные разделы сайта, не опечатки."""
    html = b'href="/skill/yandex-direct" href="/cli/coddy-agent" ' \
           b'href="/mcp/rusender-mcp" href="/skills/validator"'
    found = {kind.decode() for kind, _ in _HREF.findall(html)}
    assert {"mcp", "skill", "cli"} <= found, found


def test_rsc_stream_provides_full_records(monkeypatch):
    """Из RSC-потока читаются имя, тип, репозиторий и описание."""
    monkeypatch.setattr(NeuralDeepSource, "_fetch",
                        lambda self, path: HTML_FIXTURE.encode("utf-8"))
    src = NeuralDeepSource(timeout=1)
    records = src._load_rsc_records()
    names = {r["name"] for r in records}
    assert "1c-log-checker" in names, names
    assert "yandex-office" in names, names
    y = next(r for r in records if r["name"] == "yandex-office")
    assert y["type"] == "mcp"
    assert y["owner"] == "bizyumov"
    assert "Яндекс" in y["description"]


def test_search_returns_every_record_not_two(monkeypatch):
    """Каталог показывает все записи, а не пару случайных href.

    В фикстуре три RSC-записи и четыре href-ссылки, из которых одна
    (`/skills/validator`) отбрасывается. Прежний код нашёл бы две
    ссылки и потерял бы описание/тип, поэтому проверяем именно полный
    набор из потока.
    """
    monkeypatch.setattr(NeuralDeepSource, "_fetch",
                        lambda self, path: HTML_FIXTURE.encode("utf-8"))
    src = NeuralDeepSource(timeout=1)
    res = src.search("", limit=100)
    assert res["total"] == 3, res["total"]
    assert src.status() == ("ok", None)
    names = {c["name"] for c in res["items"]}
    assert names == {"1c-log-checker", "yandex-office", "coddy-agent"}, names


def test_search_matches_on_description(monkeypatch):
    """Поиск идёт по описанию, а не только по имени."""
    monkeypatch.setattr(NeuralDeepSource, "_fetch",
                        lambda self, path: HTML_FIXTURE.encode("utf-8"))
    src = NeuralDeepSource(timeout=1)
    res = src.search("яндекс", limit=10)
    assert res["total"] >= 1, res
    assert res["items"][0]["name"] == "yandex-office"
