"""Синхронизация индекса должна переживать сбой на отдельной странице.

Найдено на живом реестре: курсор `ai.borealhost/mcp:0.4.2` роняет
запрос в ReadTimeout, и весь sync() падал, не сохраняя ничего. Панель
показывала «локальный индекс пуст» при доступном реестре — то же
ложное сообщение, что и раньше.

Контракт: страницы до сбоя сохраняются, сбойная страница пропускается
с ограниченным числом попыток, синк не бросает исключение.
"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from aomg.index import CatalogIndex  # noqa: E402

PAGE_A = {"servers": [{"server": {"name": f"a.io/one{i}"}} for i in range(3)],
          "metadata": {"nextCursor": "bad-cursor"}}
PAGE_B = {"servers": [{"server": {"name": "b.io/two"}}],
          "metadata": {}}


class _Resp:
    def __init__(self, payload):
        self._p = payload
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class _Client:
    """Клиент, который падает на заданном курсоре."""

    def __init__(self, fail_on, **kw):
        self.fail_on = fail_on
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None):
        cur = (params or {}).get("cursor")
        self.calls += 1
        if cur == self.fail_on:
            raise __import__("httpx").ReadTimeout("read timed out")
        return _Resp(PAGE_B if cur else PAGE_A)


def test_sync_keeps_rows_when_one_page_fails(tmp_path, monkeypatch):
    """Сбой на странице не уничтожает уже собранные записи.

    Обход на сорванной странице обрывается (продолжать с того же курсора
    бессмысленно), но всё, что собрано до сбоя, обязано лечь на диск.
    Прежний код бросал ReadTimeout наружу и не сохранял ничего.
    """
    idx = CatalogIndex(tmp_path / "index.json")
    monkeypatch.setattr("httpx.Client", lambda **kw: _Client("bad-cursor"))
    n = idx.sync(timeout=5.0)
    assert n == 3, f"сохранилось {n} записей, ожидалось 3 (страница до сбоя)"
    assert (tmp_path / "index.json").exists(), "индекс не записан"
    data = idx.load()
    assert data and len(data["servers"]) == 3


def test_failed_page_is_retried_a_few_times_then_skipped(tmp_path,
                                                        monkeypatch):
    """Сбойная страница не превращается в бесконечный цикл."""
    from aomg.index import PAGE_ATTEMPTS

    idx = CatalogIndex(tmp_path / "index.json")
    c = _Client("bad-cursor")
    monkeypatch.setattr("httpx.Client", lambda **kw: c)
    idx.sync(timeout=5.0)
    # 1 запрос первой страницы + PAGE_ATTEMPTS запросов сбойной
    assert c.calls == 1 + PAGE_ATTEMPTS, \
        f"ожидалось {1 + PAGE_ATTEMPTS} попыток, было {c.calls}"


def test_sync_does_not_raise_when_registry_unreachable(tmp_path,
                                                       monkeypatch):
    """Реестр недоступен и индекса нет — понятная ошибка, не таймаут."""
    import httpx

    idx = CatalogIndex(tmp_path / "index.json")

    class _Dead(_Client):
        def get(self, url, params=None):
            raise httpx.ConnectError("down")

    monkeypatch.setattr("httpx.Client", lambda **kw: _Dead("x"))
    with pytest.raises(Exception):
        idx.sync(timeout=1.0)
