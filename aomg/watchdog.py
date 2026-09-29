"""Watchdog: периодический initialize + tools/list через egress группы.

Различает: ok / reconnecting (ребёнок перезапускается) /
channel_down (egress недоступен) / down (апстрим отвечает ошибкой).
"""
from __future__ import annotations

import json
import threading
import time

import httpx

from .config import Config
from .health import Health
from .supervisor import Supervisor


def _json_or_sse(text: str) -> dict:
    """JSON-тело или SSE (event: message\\ndata: {...})."""
    text = text.strip()
    if text.startswith("{"):
        return json.loads(text)
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise ValueError(f"no data in response: {text[:80]}")


def probe(base: str, name: str, proxy: str | None,
          timeout: float = 15.0) -> tuple[str, int, str | None]:
    """Возвращает (state, tools_count, error)."""
    headers = {"Accept": "application/json, text/event-stream",
               "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=timeout, proxy=proxy, trust_env=False) as c:
            r = c.post(f"{base}/{name}/mcp", json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05",
                           "capabilities": {},
                           "clientInfo": {"name": "aomg-watchdog",
                                          "version": "0.1"}}},
                headers=headers)
            if r.status_code in (502, 504):
                return ("channel_down" if r.status_code == 504 else "down",
                        0, r.text[:200])
            sid = r.headers.get("mcp-session-id")
            if sid:
                headers["mcp-session-id"] = sid
            c.post(f"{base}/{name}/mcp", json={
                "jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=headers)
            tr = c.post(f"{base}/{name}/mcp",
                        json={"jsonrpc": "2.0", "id": 2,
                              "method": "tools/list"},
                        headers=headers)
            data = _json_or_sse(tr.text)
            if "error" in data:
                return "down", 0, str(data["error"])[:200]
            tools = data.get("result", {}).get("tools", [])
            return "ok", len(tools), None
    except httpx.ConnectError as e:
        return "channel_down", 0, str(e)[:200]
    except Exception as e:
        return "down", 0, f"{type(e).__name__}: {e}"[:200]


def watch_loop(cfg: Config, supervisor: Supervisor,
               healths: dict[str, Health], interval: float = 30.0,
               base: str | None = None) -> None:
    base = base or f"http://127.0.0.1:{cfg.gateway_port}"
    while True:
        for name, spec in cfg.servers.items():
            h = healths[name]
            if spec.kind == "stdio" and not supervisor.managed[name].alive():
                h.record("reconnecting", ts=time.time(),
                         error="child dead, restarting")
                supervisor.restart(name)
                continue
            state, tools, err = probe(base, name, cfg.egress_for(spec))
            h.record(state, tools=tools, error=err, ts=time.time())
        time.sleep(interval)


def start_watchdog(cfg: Config, supervisor: Supervisor,
                   healths: dict[str, Health], interval: float = 30.0,
                   base: str | None = None) -> threading.Thread:
    t = threading.Thread(
        target=watch_loop,
        args=(cfg, supervisor, healths, interval, base), daemon=True)
    t.start()
    return t
