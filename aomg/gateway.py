"""HTTP-гейтвей: /<name>/mcp -> апстрим (ребёнок-прокси или удалённый URL),
/health -> агрегат для иконки и тестов.

Proxy-режим: полный проход HTTP с телом/заголовками, апстрим-клиент
выбирается по egress группы (None = прямой).
"""
from __future__ import annotations

import threading
import time

import httpx
from fastapi import FastAPI, Request, Response

from .config import Config
from .health import Health, aggregate_state
from .supervisor import Supervisor

HOP_HEADERS = {"content-length", "transfer-encoding", "connection",
               "keep-alive", "host"}


def create_app(cfg: Config, supervisor: Supervisor,
               healths: dict[str, Health]) -> FastAPI:
    app = FastAPI(title="AOMG")
    clients: dict[str | None, httpx.AsyncClient] = {}

    def client_for(proxy: str | None) -> httpx.AsyncClient:
        if proxy not in clients:
            clients[proxy] = httpx.AsyncClient(
                timeout=httpx.Timeout(120.0, read=300.0), proxy=proxy,
                trust_env=False)  # NO_PROXY только loopback, egress задаём явно
        return clients[proxy]

    async def proxy_mcp(request: Request) -> Response:
        name = request.path_params["name"]
        spec = cfg.servers.get(name)
        if spec is None:
            return Response(f"unknown mcp: {name}", status_code=404)
        upstream = supervisor.upstream_for(name)
        if upstream is None:
            return Response(f"upstream for {name} is down", status_code=502)
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in HOP_HEADERS}
        # авторизация из spec (записана в конфиге) поверх клиентских
        for k, v in spec.headers.items():
            headers[k] = v
        body = await request.body()
        try:
            # асинхронный клиент: НЕ блокируем event loop долгими
            # MCP-соединениями (иначе один SSE-клиент вешает весь gateway)
            r = await client_for(cfg.egress_for(spec)).request(
                request.method, upstream, content=body, headers=headers)
            resp_headers = {k: v for k, v in r.headers.items()
                            if k.lower() not in HOP_HEADERS}
            return Response(r.content, status_code=r.status_code,
                            headers=resp_headers)
        except httpx.ConnectError as e:
            healths[name].record("channel_down", error=str(e), ts=time.time())
            return Response(f"channel down: {e}", status_code=504)

    for name in cfg.servers:
        app.add_api_route("/{name}/mcp", proxy_mcp, methods=[
            "GET", "POST", "DELETE"])

    @app.get("/health")
    def health() -> dict:
        from .supervisor import last_error
        return {"aggregate": aggregate_state(list(healths.values())),
                "servers": [{"name": n, "state": h.state, "tools": h.tools,
                             "pid": h.pid, "error": h.error,
                             "log_tail": last_error(n)}
                            for n, h in healths.items()]}

    @app.get("/admin/api/logs/{name}")
    def server_log(name: str) -> dict:
        """Полный лог сервера (для панели/отладки)."""
        from .supervisor import logs_dir
        p = logs_dir() / f"{name}.log"
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        return {"name": name, "log": text[-20000:]}

    @app.post("/admin/restart/{name}")
    def restart(name: str) -> dict:
        supervisor.restart(name)
        return {"restarted": name}

    return app


def serve_in_thread(cfg: Config, supervisor: Supervisor,
                    healths: dict[str, Health],
                    configure=None) -> threading.Thread:
    """Запуск uvicorn в треде. configure(app) вызывается ДО старта."""
    import uvicorn
    app = create_app(cfg, supervisor, healths)
    if configure:
        configure(app)
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=cfg.gateway_port, log_level="warning"))

    t = threading.Thread(target=server.run, daemon=True)
    t.app = app  # доступ из run.py (админка)
    t.start()
    return t
