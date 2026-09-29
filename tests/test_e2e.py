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

    env = {"AOMG_CONFIG": str(cfg_file)}
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
    data = httpx.get(f"{gateway}/health", timeout=5).json()
    pid = next(s for s in data["servers"] if s["name"] == "fake")["pid"]
    subprocess.run(["taskkill", "/F", "/PID", str(pid)], check=False)
    time.sleep(4)  # супервизор должен заметить и поднять
    result = _tools_list(gateway, "fake")
    tools = [t["name"] for t in result["result"]["tools"]]
    assert "fake_echo" in tools, "после смерти ребёнка гейтвей снова отвечает"


def test_unknown_path_404(gateway):
    assert httpx.post(f"{gateway}/nosuch/mcp", json=_rpc("tools/list"),
                      timeout=5).status_code == 404
