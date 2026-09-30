"""E2E: гейтвей поднимается, stdio-ребёнок жив, tools/list сквозь гейтвей.

Фейковый stdio MCP-сервер запускается как subprocess (как настоящий),
gateway агрегирует его на /fake/mcp, клиент ходит по HTTP.

Запуск: python -m pytest tests/test_e2e.py -v
"""
import json
import pathlib
import subprocess
import sys
import time

import httpx
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAKE_SERVER = ROOT / "tests" / "fixtures" / "fake_stdio_server.py"
GATEWAY_PORT = 9377  # не пересекаться с боевым 9300


def _rpc(method, params=None, _id=1):
    msg = {"jsonrpc": "2.0", "id": _id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


@pytest.fixture(scope="module")
def gateway(tmp_path_factory):
    cfg_file = tmp_path_factory.mktemp("aomg") / "config.yaml"
    cfg_file.write_text(
        "gateway_port: %d\n"
        "groups:\n"
        "  direct: {name: 'Дом', proxy: null}\n"
        "servers:\n"
        "  fake:\n"
        "    command: \"%s\"\n"
        "    args: [\"%s\"]\n"
        "    group: direct\n"
        % (GATEWAY_PORT, sys.executable.replace("\\", "/"), str(FAKE_SERVER).replace("\\", "/")),
        encoding="utf-8")

    env = {"AOMG_CONFIG": str(cfg_file),
           # watchdog раз в секунду: тест рестарта обязан уложиться
           # в разумное время, а боевой дефолт — 30 с
           "AOMG_WATCH_INTERVAL": "1"}
    import os
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "run.py"), "--no-tray"],
        env={**os.environ, **env},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{GATEWAY_PORT}"
    try:
        for _ in range(50):
            try:
                if httpx.get(f"{base}/health", timeout=1).status_code == 200:
                    break
            except Exception:
                time.sleep(0.3)
        else:
            proc.terminate()
            out = proc.stdout.read().decode(errors="replace")
            raise RuntimeError(f"gateway not up: {out[-2000:]}")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        # дети (mcp-proxy + stdio-сервер) переживают родителя: без этого
        # они копятся между прогонами и держат файлы сборки открытыми
        out = subprocess.run(["wmic", "process", "get", "processid,commandline"],
                             capture_output=True)
        raw = out.stdout
        txt = (raw.decode("utf-16-le", errors="replace")
               if raw[:2] == b"\xff\xfe" else raw.decode(errors="replace"))
        for line in txt.splitlines():
            low = line.lower()
            if "fake_stdio" not in low and "mcp_proxy --port" not in low:
                continue
            parts = line.split()
            if parts and parts[-1].isdigit() and int(parts[-1]) > 3:
                subprocess.run(["taskkill", "/F", "/PID", parts[-1]],
                               capture_output=True)


def _tools_list(base, name):
    headers = {"Accept": "application/json, text/event-stream",
               "Content-Type": "application/json"}
    with httpx.Client(timeout=15) as c:
        init = c.post(f"{base}/{name}/mcp", json=_rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {}, "clientInfo": {"name": "e2e", "version": "0"}}),
            headers=headers)
        assert init.status_code == 200, init.text
        sid = init.headers.get("mcp-session-id")
        if sid:
            headers["mcp-session-id"] = sid
        c.post(f"{base}/{name}/mcp",
               json={"jsonrpc": "2.0", "method": "notifications/initialized"},
               headers=headers)
        r = c.post(f"{base}/{name}/mcp", json=_rpc("tools/list"),
                   headers=headers).json()
    return r


def test_gateway_aggregates_stdio_child(gateway):
    result = _tools_list(gateway, "fake")
    tools = [t["name"] for t in result["result"]["tools"]]
    assert "fake_echo" in tools


def test_gateway_health_reports_ok(gateway):
    time.sleep(1)
    data = httpx.get(f"{gateway}/health", timeout=5).json()
    entry = next(s for s in data["servers"] if s["name"] == "fake")
    assert entry["state"] in ("ok", "reconnecting")
    assert entry["tools"] >= 1


def test_child_restart_after_kill(gateway):
    """Ребёнок убит — watchdog обязан поднять новый, и PID обязан смениться.

    Проверяем не «через 4 с стало ок», а «новый процесс действительно
    другой»: иначе тест проходил бы, когда рестарт не случился вовсе.
    """
    data = httpx.get(f"{gateway}/health", timeout=5).json()
    old_pid = next(s for s in data["servers"] if s["name"] == "fake")["pid"]
    assert old_pid, "у живого stdio-сервера обязан быть pid"
    subprocess.run(["taskkill", "/F", "/PID", str(old_pid)], check=False)

    # ждём новый pid, а не фиксированное время
    deadline = time.time() + 20.0
    new_pid = old_pid
    while time.time() < deadline:
        time.sleep(0.5)
        try:
            body = httpx.get(f"{gateway}/health", timeout=5).json()
        except Exception:
            continue
        entry = next((s for s in body["servers"]
                      if s["name"] == "fake"), None)
        if entry and entry.get("pid") and entry["pid"] != old_pid:
            new_pid = entry["pid"]
            break
    assert new_pid != old_pid, \
        f"ребёнок не перезапущен: pid остался {old_pid} — watchdog молчит"

    # и канал снова работает
    tools = None
    deadline = time.time() + 20.0
    while time.time() < deadline and tools is None:
        try:
            result = _tools_list(gateway, "fake")
            tools = [t["name"] for t in result["result"]["tools"]]
        except AssertionError:
            time.sleep(0.5)
    assert tools and "fake_echo" in tools, \
        "после смерти ребёнка гейтвей снова отвечает"


def test_unknown_path_404(gateway):
    assert httpx.post(f"{gateway}/nosuch/mcp", json=_rpc("tools/list"),
                      timeout=5).status_code == 404
