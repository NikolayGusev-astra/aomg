"""Каталог: контракт источников (ADR-0005).

Тест не «просто импортируется», а проверяет инварианты, которые в
аудите и сломались:

* `NeuralDeepSource` отдаёт `search()`/`status()` — тот же контракт,
  что у JsonSource и официального адаптера. Раньше здесь был второй
  несовместимый интерфейс (`list_items`/`get_item`), а admin.py звал
  `search()` → `AttributeError` на живой сборке.
* Состояние — результат последней попытки, а не константа `ok`.
* Пагинация через offset, а не «первые N».
* Сетевые обращения замоканы: тест не ходит в интернет и не падает
  из-за сети — проверяет логику разбора.
"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from aomg.catalog import NeuralDeepSource

RSC_PAGE = rb'''
<script>self.__next_f.push([1,"...\"href\":\"/mcp/rusender-mcp\"..."])</script>
<script>self.__next_f.push([1,"...\"href\":\"/mcp/spring-ssh-mcp\"..."])</script>
<script>self.__next_f.push([1,"...\"href\":\"/skills/spb-gorzdrav-skill\"..."])</script>
<script>self.__next_f.push([1,"...\"href\":\"/skills/validator\"..."])</script>
<script>self.__next_f.push([1,"...\"href\":\"/mcp/rusender-mcp\"..."])</script>
'''

ITEM_PAGE = b'''<html><h1>rusender-mcp</h1>
MCP-servers RuSender email automation.
npx skillsbd add Rusender/rusender-mcp/rusender-mcp
github.com/Rusender/rusender-mcp
</html>'''


@pytest.fixture
def nd(monkeypatch):
    src = NeuralDeepSource()

    def fake_fetch(path):
        if path == "/skills":
            return RSC_PAGE
        if path.startswith("/mcp/") or path.startswith("/skills/"):
            return ITEM_PAGE
        raise AssertionError(f"незапланированный запрос {path}")

    monkeypatch.setattr(src, "_fetch", fake_fetch)
    return src


# ---- контракт ----

def test_source_implements_unified_source_contract(nd):
    """Единственный интерфейс источника каталога (ADR-0005 I1)."""
    for method in ("search", "status"):
        assert callable(getattr(nd, method, None)), \
            f"у источника каталога нет {method}() — панель зовёт только их"


def test_search_returns_pagination_envelope(nd):
    res = nd.search("", limit=50, offset=0)
    assert set(res) == {"items", "total", "has_more"}, \
        "контракт источника: {items, total, has_more}"
    assert isinstance(res["items"], list)
    assert res["total"] == len(res["items"])


def test_search_paginates_with_offset(nd):
    page1 = nd.search("", limit=1, offset=0)
    page2 = nd.search("", limit=1, offset=1)
    assert page1["has_more"] is True
    assert len(page1["items"]) == 1
    assert page1["items"][0] is not page2["items"][0], \
        "offset обязан двигать окно, а не отдавать то же самое"


def test_query_filters_items(nd):
    res = nd.search("gorzdrav", limit=50)
    assert res["items"], "поиск по названию должен что-то находить"
    assert all("gorzdrav" in c["name"].lower() for c in res["items"])


# ---- разбор данных ----

def test_parses_slugs_skips_validator_and_dedupes(nd):
    names = {(c["kind"], c["name"]) for c in nd.search("")["items"]}
    assert ("mcp", "rusender-mcp") in names
    assert ("skills", "spb-gorzdrav-skill") in names
    assert ("skills", "validator") not in names, "validator служебный"
    assert len(names) == 3, "дубликаты href должны схлопываться"


def test_item_page_yields_install_command_and_repo(nd):
    card = next(c for c in nd.search("")["items"]
                if c["name"] == "rusender-mcp")
    assert card["command"] == "npx"
    assert "skillsbd" in " ".join(card["args"])
    assert card["repo_url"] == "https://github.com/Rusender/rusender-mcp"
    assert card["description"], "описание берётся из текста страницы"


def test_every_item_is_pickable_for_the_panel(nd):
    """Карточка из каталога должна быть пригодна для формы добавления."""
    for c in nd.search("")["items"]:
        assert c["name"]
        assert c["source"] == "neuraldeep", "источник помечен явно"
        assert isinstance(c["args"], list)


# ---- правдивый статус ----

def test_state_is_unprobed_before_first_call():
    """До первой попытки честное `unprobed`, а не `ok` (ADR-0005 I6).

    Раньше источник светился зелёным, оставаясь нерабочим.
    """
    src = NeuralDeepSource()
    assert src.status() == ("unprobed", None)


def test_unreachable_source_reports_error_not_ok(monkeypatch):
    src = NeuralDeepSource()

    def boom(path):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(src, "_fetch", boom)
    res = src.search("", limit=10)
    state, err = src.status()
    assert state != "ok", "недоступный источник не может быть зелёным"
    assert err and "ConnectionError" in err
    assert res["items"] == [], "недоступный источник не отдаёт выдуманных карточек"


def test_cards_are_cached_between_searches(nd, monkeypatch):
    """Второй поиск не должен заново ходить в сеть."""
    nd.search("")
    calls = []
    real = nd._fetch
    monkeypatch.setattr(nd, "_fetch",
                        lambda p: (calls.append(p), real(p))[1])
    nd.search("rusender")
    nd.search("spring")
    assert calls == [], "карточки уже разобраны — сеть не нужна"


def test_status_survives_repeated_unreachable_searches(monkeypatch):
    """Повторные попытки не роняют источник и не теряют статус."""
    src = NeuralDeepSource()

    def timeout(path):
        raise TimeoutError("t")

    monkeypatch.setattr(src, "_fetch", timeout)
    for _ in range(3):
        src.search("", limit=10)
    state, err = src.status()
    assert state != "ok" and "TimeoutError" in err
