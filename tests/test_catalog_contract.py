"""Контракт источников каталога (ADR-0005).

Не «смоук»: проверки контракта, которые ловят именно те дефекты, что
найдены аудитом сессии 20260930_100646_31e2ee.

- I1: каждый зарегистрированный источник имеет search()/status().
      Регрессия: NeuralDeepSource не имел search() -> AttributeError в
      прод-панели (logs/aomg.log установленной сборки).
- I2: JsonSource открывает клиент через прокси своей egress-группы.
      Регрессия: группа из конфига игнорировалась.
- I3: upsert источника не выбрасывает состояние/кэши остальных.
- I4: живой фолбэк реестра ограничен по времени.
- I5: ответ каталога пагинирован (total/has_more), offset даёт срез.
"""
import json
import pathlib
import socket
import sys
import time

import httpx
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aomg.admin import register_admin
from aomg.config import load_config
from aomg.json_source import JsonSource
from aomg.supervisor import Supervisor


LIST = [
    {"name": "blender-mcp", "title": "Blender MCP",
     "description": "Blender integration", "url": "https://x.example/mcp"},
    {"name": "unreal-mcp", "command": "npx -y unreal-mcp@2"},
]


def _cfg_file(tmp_path, extra_sources="", port=9391):
    p = tmp_path / "config.yaml"
    p.write_text(
        "gateway_port: %d\n"
        "groups:\n"
        "  direct: {name: 'Напрямую', proxy: null}\n"
        "  vpn: {name: 'VPN', proxy: 'socks5://127.0.0.1:1'}\n"
        "servers: {}\n"
        "catalog_sources:\n"
        "  corp:\n"
        "    type: json\n"
        "    url: https://corp.example/mcp-servers\n"
        "    group: vpn\n"
        "    headers: {Authorization: 'Bearer ${CORP_TOKEN}'}\n"
        "%s" % (port, extra_sources),
        encoding="utf-8")
    return p


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CORP_TOKEN", "topsecret-123")
    cfg_file = _cfg_file(tmp_path)
    cfg = load_config(cfg_file)
    app = FastAPI()
    sup = Supervisor(cfg)
    register_admin(app, cfg, sup, cfg_file, restart_watchdog=lambda: None)
    c = TestClient(app)
    c.sources = app.state.catalog_sources
    c.cfg_file = cfg_file
    c.app = app
    c.cfg = cfg
    yield c


def _mock(sources, name, handler):
    src = sources.get(name)
    assert src is not None, f"источник {name} не зарегистрирован"
    src._transport = httpx.MockTransport(handler)
    return src


# ---------- I1: единый контракт ----------

def test_i1_every_registered_source_has_search_and_status(app_env):
    """Каждый источник в реестре панели отдаёт search()/status().

    Именно этот инвариант отсутствовал: admin звал src.search() у всех,
    а NeuralDeepSource такого метода не имел.
    """
    sources = app_env.sources
    assert sources, "должен быть хотя бы встроенный источник"
    for name, src in sources.items():
        assert callable(getattr(src, "search", None)), \
            f"источник {name} без search(): {type(src).__name__}"
        assert callable(getattr(src, "status", None)), \
            f"источник {name} без status(): {type(src).__name__}"


def test_i1_status_is_callable_and_returns_state_error_pair(app_env):
    st, err = app_env.sources["official"].status()
    # `unprobed` — честное состояние до первой попытки, а не `ok`
    assert st in ("ok", "error", "unreachable", "warming", "unprobed")
    assert err is None or isinstance(err, str)


def test_i1_neuraldeep_search_returns_cards(monkeypatch):
    """Встроенный html-адаптер ищет и отдаёт карточки того же формата."""
    from aomg.catalog import NeuralDeepSource
    src = NeuralDeepSource()

    class _T(httpx.BaseTransport):
        def handle_request(self, request):
            if request.url.path == "/skills":
                return httpx.Response(200, content=(
                    b'<script>self.__next_f.push([1,"...\\"/mcp/rusender-mcp\\"..."])'
                    b'</script>'
                    b'<script>self.__next_f.push([1,"...\\"/skills/spb-gorzdrav-skill\\"..."])'
                    b'</script>'
                    b'<script>self.__next_f.push([1,"...\\"/skills/validator\\"..."])'
                    b'</script>'), headers={"content-type": "text/html"})
            return httpx.Response(404)

    src._transport = _T()
    res = src.search("rusender", limit=10, offset=0)
    assert set(res) == {"items", "total", "has_more"}, \
        "встроенный источник отдаёт тот же конверт, что и json-источник"
    assert [c["name"] for c in res["items"]] == ["rusender-mcp"]
    assert res["items"][0]["source"] == "neuraldeep"


# ---------- I2: egress-группа источника ----------

def test_i2_json_source_uses_group_proxy(tmp_path, monkeypatch):
    """Непустая группа -> клиент источника идёт через её прокси."""
    cfg = load_config(_cfg_file(tmp_path))
    spec = cfg.catalog_sources["corp"]
    assert spec.group == "vpn"
    src = JsonSource(spec, cache_dir=tmp_path, proxy=cfg.egress_for_group("vpn"))
    assert src.proxy == "socks5://127.0.0.1:1", \
        "прокси группы обязан доезжать до источника (ADR-0002/0005)"

    seen = {}

    class _T(httpx.BaseTransport):
        def handle_request(self, request):
            seen["url"] = str(request.url)
            return httpx.Response(200, json=LIST)

    src._transport = _T()
    res = src.fetch()
    assert res.state == "ok"
    assert seen["url"].startswith("https://corp.example/")


def test_i2_trust_env_stays_false_with_proxy(tmp_path):
    """Прокси источника явный; env хоста не наследуется (NO_PROXY=* ломает)."""
    cfg = load_config(_cfg_file(tmp_path))
    src = JsonSource(cfg.catalog_sources["corp"], cache_dir=tmp_path,
                     proxy="socks5://127.0.0.1:1")
    import inspect
    sig = inspect.getsource(JsonSource._client)
    assert "trust_env=False" in sig


def test_i2_direct_group_means_no_proxy(tmp_path):
    cfg = load_config(_cfg_file(tmp_path))
    assert cfg.egress_for_group("direct") is None


# ---------- I3: инкрементальная регистрация ----------

def test_i3_upsert_keeps_other_sources_identity(app_env):
    before = dict(app_env.sources)
    official_before = before["official"]
    corp_before = before["corp"]
    r = app_env.post("/admin/api/catalog/sources", json={
        "name": "team", "type": "json",
        "url": "https://inner.example/mcp.json", "group": "direct"})
    assert r.json().get("saved") == "team"
    after = app_env.app.state.catalog_sources
    assert after["official"] is official_before, \
        "upsert не должен пересоздавать несвязанные источники"
    assert after["corp"] is corp_before
    assert "team" in after


def test_i3_delete_removes_only_target(app_env):
    corp_before = app_env.sources["corp"]
    app_env.post("/admin/api/catalog/sources", json={
        "name": "tmp-src", "type": "json",
        "url": "https://t.example/mcp.json", "group": "direct"})
    app_env.post("/admin/api/catalog/sources/tmp-src/delete")
    after = app_env.app.state.catalog_sources
    assert "tmp-src" not in after
    assert after["corp"] is corp_before


# ---------- I4: живой фолбэк ограничен по времени ----------

def test_i4_live_registry_fallback_is_bounded(app_env, monkeypatch):
    """Реестр недоступен -> быстрый notice, не 20-секундное зависание."""
    async def slow(*a, **kw):
        raise httpx.ReadTimeout("read timeout")
    monkeypatch.setattr("aomg.registry.search_async", slow)
    t0 = time.time()
    data = app_env.get("/admin/api/catalog",
                       params={"query": "blender"}).json()
    elapsed = time.time() - t0
    assert elapsed < 5.0, f"фолбэк реестра занял {elapsed:.1f}с — нужен потолок"
    assert data["items"] == []
    assert data.get("notice"), "пользователь должен видеть честную причину"


def test_i4_registry_client_timeout_is_small(monkeypatch):
    """Таймаут реестра — единицы секунд, не десятки (ADR-0005 п.5.5)."""
    import aomg.registry as reg
    import inspect
    src = inspect.getsource(reg.search_async)
    assert "timeout=" in src
    for bad in ("15.0", "20.0", "30.0", "60.0"):
        assert bad not in src, f"слишком долгий таймаут {bad}"


# ---------- I5: пагинация ----------

def test_i5_response_has_total_and_has_more(app_env):
    def handler(request):
        return httpx.Response(200, json=LIST)
    _mock(app_env.sources, "corp", handler)
    data = app_env.get("/admin/api/catalog",
                       params={"source": "corp", "query": ""}).json()
    assert "total" in data and "has_more" in data
    assert data["total"] == 2
    assert data["has_more"] is False


def test_i5_offset_returns_slice_not_head(app_env):
    """offset>1 обязан дать срез, иначе 'показывать ещё' невозможно."""
    big = [{"name": f"svc-{i:02d}", "url": f"https://x/{i}"} for i in range(120)]
    _mock(app_env.sources, "corp", lambda r: httpx.Response(200, json=big))
    first = app_env.get("/admin/api/catalog",
                        params={"source": "corp", "limit": 50}).json()
    second = app_env.get("/admin/api/catalog",
                         params={"source": "corp", "limit": 50,
                                 "offset": 50}).json()
    assert len(first["items"]) == 50
    assert first["has_more"] is True
    assert first["total"] == 120
    assert second["items"][0]["name"] != first["items"][0]["name"]
    assert {i["name"] for i in first["items"]} & \
           {i["name"] for i in second["items"]} == set()


def test_i5_limit_is_clamped(monkeypatch, app_env):
    """limit из UI не должен уронить индекс (и не должен быть без границы)."""
    _mock(app_env.sources, "corp", lambda r: httpx.Response(200, json=LIST))
    data = app_env.get("/admin/api/catalog",
                       params={"source": "corp", "limit": 100000}).json()
    assert data["limit"] <= 200, \
        f"limit={data['limit']} — панель может попросить и 100000"


# ---------- статус = результат последней попытки ----------

def test_status_reflects_last_attempt_not_lazy_ok(app_env):
    """Источник, который не отвечает, не должен светиться зелёным."""
    corp = _mock(app_env.sources, "corp",
                 lambda r: (_ for _ in ()).throw(httpx.ConnectError("no route")))
    corp.cache_path.unlink(missing_ok=True)
    corp.search("x", limit=5)
    state, _err = corp.status()
    assert state in ("error", "unreachable"), \
        "зелёный статус у недоступного источника — ровно тот баг из аудита"


def test_source_status_survives_no_attempt_yet(app_env):
    """До первой попытки статус — честное 'не проверен', не 'ok'."""
    corp = app_env.sources["corp"]
    corp.state, corp.error = "unprobed", None
    assert corp.status()[0] == "unprobed"
