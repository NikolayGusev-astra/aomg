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


def make_prober(cfg: Config, base: str):
    """Замыкание probe(name) -> (state, tools, error) для Supervisor."""
    def _probe(name: str):
        spec = cfg.servers.get(name)
        proxy = cfg.egress_for(spec) if spec else None
        return probe(base, name, proxy)
    return _probe


def watch_once(supervisor: Supervisor, base: str | None = None) -> None:
    """Один проход: проба всех серверов + политика для упавших детей."""
    base = base or f"http://127.0.0.1:{supervisor.cfg.gateway_port}"
    supervisor.watch_tick(time.time(),
                          probe=make_prober(supervisor.cfg, base))


def watch_loop(cfg: Config, supervisor: Supervisor,
               interval: float = 30.0, base: str | None = None) -> None:
    """Наблюдатель, а не политик.

    Перезапуск и backoff живут в Supervisor (ADR-0004); владение
    состоянием — тоже там (ADR-0003). Здесь только проба и передача
    фактов, поэтому цикл не может умереть от одного сервера, которого
    нет в реестре, и не падает целиком на исключении в проходе.
    """
    base = base or f"http://127.0.0.1:{cfg.gateway_port}"
    while True:
        try:
            watch_once(supervisor, base)
        except Exception:
            pass
        time.sleep(interval)


def start_watchdog(cfg: Config, supervisor: Supervisor,
                   interval: float = 30.0,
                   base: str | None = None) -> threading.Thread:
    t = threading.Thread(
        target=watch_loop,
        args=(cfg, supervisor, interval, base), daemon=True)
    t.start()
    return t
