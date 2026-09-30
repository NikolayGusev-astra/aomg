"""E2E на СОБРАННОМ exe, а не на исходниках.

Зачем это отдельным файлом: `tests/test_e2e.py` гоняет `run.py` из
исходников и ничего не говорит о том, запускается ли поставка.
Ровно это и ломалось в предыдущих релизах: тесты зелёные, инсталлятор
собран, а у юзера `ModuleNotFoundError` из frozen-приложения.

Проверяем ровно то, что ломалось в проде:
1. frozen exe стартует без консоли (stdout == None) и не падает;
2. порт гейтвея выбирается и пишется в автоконфиг;
3. stdio-ребёнок поднимается, tools/list проходит через гейтвей;
4. ребёнок убит — watchdog поднимает новый (новый pid);
5. example-конфиг в поставке не превращается в рабочий.

Пропускается (не падает), если exe не собран: `python -m pytest tests`
на dev-машине без PyInstaller-сборки должен оставаться зелёным.
Запуск со сборкой: `python -m pytest tests/test_frozen_e2e.py`.
"""
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import time

import httpx
import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "dist" / "AOMG"
EXE = BUNDLE / "AOMG.exe"
PROXY_EXE = BUNDLE / "mcp-proxy.exe"
FAKE_SERVER = ROOT / "tests" / "fixtures" / "fake_stdio_server.py"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _occupy(port: int) -> socket.socket:
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(1)
    return s


def _rpc(method, params=None, _id=1):
    msg = {"jsonrpc": "2.0", "id": _id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


pytestmark = pytest.mark.skipif(
    not (EXE.exists() and PROXY_EXE.exists()),
    reason="frozen bundle не собран: сначала pyinstaller AOMG.spec "
           "и mcpproxy.spec")


@pytest.fixture(scope="module")
def frozen(tmp_path_factory):
    """Ставит собранный бандл во временную папку и запускает exe.

    Копируем, а не запускаем из dist/: приложение пишет рядом с exe
    config.yaml, registry-index.json и логи — иначе сборка засоряется
    состоянием между прогонами и тест зависит от предыдущего.
    """
    app = tmp_path_factory.mktemp("frozen")
    exe = app / "AOMG.exe"
    shutil.copy2(EXE, exe)
    # mcp-proxy и его internal-каталог — рядом, иначе спавн не найдёт
    proxy = app / "mcp-proxy.exe"
    shutil.copy2(PROXY_EXE, proxy)
    src_internal = BUNDLE / "mcp_proxy_internal"
    if src_internal.exists():
        shutil.copytree(src_internal, app / "mcp_proxy_internal")
    internal = BUNDLE / "_internal"
    if internal.exists():
        shutil.copytree(internal, app / "_internal")

    env = {**os.environ, "AOMG_WATCH_INTERVAL": "1"}
    proc = subprocess.Popen([str(exe), "--no-tray"], env=env,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    # Порт НЕ задаём: приложение само выбирает свободный и пишет его
    # в config.yaml. Тест узнаёт порт из фактического конфига — иначе
    # пришлось бы угадывать, какой из тысячи свободных портов взят.
    cfg_file = app / "config.yaml"
    port = None
    deadline = time.time() + 30.0
    while time.time() < deadline and port is None:
        if proc.poll() is not None:
            out = proc.stdout.read().decode(errors="replace")
            raise RuntimeError(f"frozen exe умер сразу:\n{out[-2000:]}")
        if cfg_file.exists():
            try:
                port = (yaml.safe_load(
                    cfg_file.read_text(encoding="utf-8")) or {}
                ).get("gateway_port")
            except Exception:
                port = None
        if port is None:
            time.sleep(0.2)
    if port is None:
        proc.terminate()
        raise RuntimeError("frozen exe не создал config.yaml с портом")

    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(80):
            if proc.poll() is not None:
                out = proc.stdout.read().decode(errors="replace")
                raise RuntimeError(f"frozen exe умер сразу:\n{out[-2000:]}")
            try:
                if httpx.get(f"{base}/health", timeout=1).status_code == 200:
                    break
            except Exception:
                time.sleep(0.3)
        else:
            raise RuntimeError(f"frozen exe не поднял гейтвей на {port}")
        yield base, app, port, proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        _kill_orphans(app)


def _kill_orphans(app: pathlib.Path) -> None:
    """Убить stdio-детей, оставшихся после остановки приложения.

    Терминация AOMG.exe не гарантирует смерть mcp-proxy и его ребёнка:
    они остаются живыми, держат открытые файлы в папке сборки и
    накапливаются между прогонами (наблюдалось 200+ осиротевших
    python-процессов). Отслеживаем детей по имени exe-пути, который
    запускали, и снимаем их принудительно.
    """
    marker = app.name.lower()
    out = subprocess.run(
        ["wmic", "process", "get", "processid,commandline"],
        capture_output=True)
    raw = out.stdout
    txt = (raw.decode("utf-16-le", errors="replace")
           if raw[:2] == b"\xff\xfe" else raw.decode(errors="replace"))
    for line in txt.splitlines():
        low = line.lower()
        if marker not in low:
            continue
        if "mcp_proxy" not in low and "mcp-proxy" not in low \
                and "fake_stdio" not in low:
            continue
        parts = line.split()
        if parts and parts[-1].isdigit() and int(parts[-1]) > 3:
            subprocess.run(["taskkill", "/F", "/PID", parts[-1]],
                           capture_output=True)


def test_frozen_exe_starts_and_answers_health(frozen):
    """Самое важное: frozen-приложение вообще запускается.

        Раньше весь suite был зелёным на исходниках, а установленная
        сборка падала — разница в этом и состоит.
    """
    base, app, port, _proc = frozen
    data = httpx.get(f"{base}/health", timeout=5).json()
    assert "servers" in data and "aggregate" in data


def test_frozen_writes_clean_autoconfig_with_real_port(frozen):
    """Автоконфиг создаётся, порт в нём — фактический, демо-серверов нет."""
    _base, app, port, _proc = frozen
    cfg_file = app / "config.yaml"
    assert cfg_file.exists(), "первый запуск обязан создать config.yaml"
    raw = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
    assert raw.get("gateway_port") == port, \
        f"в конфиге {raw.get('gateway_port')}, гейтвей слушает {port}"
    assert not (raw.get("servers") or {}), \
        "в поставку нельзя класть мёртвые демо-серверы (ADR-0006)"


def test_frozen_panel_legend_has_no_hardcoded_port(frozen):
    """Легенда панели показывает ФАКТИЧЕСКИЙ порт гейтвея.

    Проверка не «9300 не встречается»: если 9300 свободен, приложение
    законно его выберет, и запрет на подстроку был бы ложным. Проверяем
    соответствие: в легенде указан порт, на котором реально слушает
    гейтвей, и никакой другой.
    """
    base, app, port, _proc = frozen
    html = httpx.get(f"{base}/admin", timeout=5).text
    legend = [ln for ln in html.splitlines() if "/mcp" in ln and "code" in ln]
    assert legend, "в панели нет строки-легенды с адресом"
    assert str(port) in legend[0], \
        f"в легенде {legend[0].strip()!r}, гейтвей слушает {port}"
    # и это не шаблон: {port} должен быть подставлен
    assert "{port}" not in html, \
        "в панели остался неподставленный плейсхолдер {port}"


def test_frozen_runtime_added_server_is_reachable(frozen):
    """Сервер, добавленный в панели frozen-сборки, доступен сразу.

    Регрессия маршрутов: они регистрировались по стартовому конфигу,
    и новый сервер получал 404 до перезапуска приложения.
    """
    base, app, _port, _proc = frozen
    port = _free_port()
    cfg_file = app / "config.yaml"
    raw = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
    raw.setdefault("servers", {})["newsrv"] = {
        "url": f"http://127.0.0.1:{port}/mcp", "group": "direct"}
    cfg_file.write_text(yaml.safe_dump(raw, allow_unicode=True),
                        encoding="utf-8")
    r = httpx.post(f"{base}/admin/api/servers",
                   json={"name": "newsrv",
                         "url": f"http://127.0.0.1:{port}/mcp",
                         "group": "direct"}, timeout=10)
    assert r.status_code == 200, r.text
    assert r.json().get("saved") == "newsrv"

    # канал до несуществующего апстрима: 502/504, но НЕ 404
    resp = httpx.post(f"{base}/newsrv/mcp", json=_rpc("initialize"),
                      timeout=10)
    assert resp.status_code in (502, 504), \
        f"ожидали 502/504, получили {resp.status_code} — маршрута нет"


def test_frozen_restarts_killed_child(frozen):
    """Ребёнок поднялся, tools/list идёт, убитый ребёнок перезапускается."""
    base, app, _port, _proc = frozen
    cfg_file = app / "config.yaml"
    raw = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
    raw.setdefault("servers", {})["fake"] = {
        "command": sys.executable.replace("\\", "/"),
        "args": [str(FAKE_SERVER).replace("\\", "/")],
        "group": "direct"}
    cfg_file.write_text(yaml.safe_dump(raw, allow_unicode=True),
                        encoding="utf-8")
    r = httpx.post(f"{base}/admin/api/servers",
                   json={"name": "fake",
                         "command": sys.executable,
                         "args": [str(FAKE_SERVER)],
                         "group": "direct"}, timeout=15)
    assert r.status_code == 200, r.text

    # ждём живой канал. Протокол MCP требует сессии: initialize ->
    # mcp-session-id -> notifications/initialized -> tools/list с тем же
    # заголовком. Без него сервер отвечает 400 на каждый следующий
    # запрос, и «ребёнок мёртв» — неверный диагноз.
    headers = {"Accept": "application/json, text/event-stream",
               "Content-Type": "application/json"}
    tools = None
    last_err = None
    deadline = time.time() + 30.0
    while time.time() < deadline and tools is None:
        try:
            init = httpx.post(f"{base}/fake/mcp", json=_rpc("initialize", {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "frozen-e2e", "version": "0"}}),
                headers=headers, timeout=10)
            if init.status_code == 200:
                sid = init.headers.get("mcp-session-id")
                h2 = dict(headers)
                if sid:
                    h2["mcp-session-id"] = sid
                httpx.post(f"{base}/fake/mcp",
                           json={"jsonrpc": "2.0",
                                 "method": "notifications/initialized"},
                           headers=h2, timeout=10)
                res = httpx.post(f"{base}/fake/mcp", json=_rpc("tools/list"),
                                 headers=h2, timeout=10)
                data = json.loads(res.text)
                if "result" in data:
                    tools = [t["name"] for t in data["result"]["tools"]]
                else:
                    last_err = res.text[:200]
            else:
                last_err = f"init {init.status_code}: {init.text[:200]}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        if tools is None:
            time.sleep(0.5)
    assert tools and "fake_echo" in tools, \
        f"frozen-сборка не отдала tools/list: {last_err}"

    # убиваем и ждём новый pid
    body = httpx.get(f"{base}/health", timeout=5).json()
    old_pid = next(s for s in body["servers"]
                   if s["name"] == "fake")["pid"]
    assert old_pid
    subprocess.run(["taskkill", "/F", "/PID", str(old_pid)], check=False)

    new_pid = old_pid
    deadline = time.time() + 30.0
    while time.time() < deadline:
        time.sleep(0.5)
        try:
            b = httpx.get(f"{base}/health", timeout=5).json()
        except Exception:
            continue
        entry = next((s for s in b["servers"] if s["name"] == "fake"), None)
        if entry and entry.get("pid") and entry["pid"] != old_pid:
            new_pid = entry["pid"]
            break
    assert new_pid != old_pid, \
        f"frozen watchdog не перезапустил ребёнка (pid {old_pid})"


def test_frozen_busy_explicit_port_exits_with_diagnostic(frozen, tmp_path):
    """Явно занятый порт -> понятная диагностика и код выхода 2.

    Молча уводить гейтвей на другой адрес нельзя: агент настроен на
    этот URL из конфига (ADR-0006).
    """
    _base, app, _port, _proc = frozen
    exe = app / "AOMG.exe"
    busy = _occupy(0)
    busy_port = busy.getsockname()[1]
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "gateway_port: %d\n"
        "groups:\n  direct: {name: 'Дом', proxy: null}\n"
        "servers: {}\n" % busy_port, encoding="utf-8")
    try:
        p = subprocess.run([str(exe), "--no-tray", "--config", str(cfg)],
                           env={**os.environ, "AOMG_WATCH_INTERVAL": "1"},
                           capture_output=True, timeout=40)
    finally:
        busy.close()
    assert p.returncode == 2, f"код выхода {p.returncode}, ожидался 2"
    out = (p.stdout or b"").decode(errors="replace")
    assert str(busy_port) in out, f"в диагностике нет порта: {out[-400:]}"
