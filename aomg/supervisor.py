"""Супервизор: stdio-дети через mcp-proxy (один прокси-процесс на сервер).

Каждый stdio-сервер получает свой mcp-proxy на свободном порту 127.0.0.1;
прокси сам спавнит и держит ребёнка. Смерть ребёнка = отказ handshake —
это ловит watchdog, рестарт = перезапуск прокси.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from .config import Config, ServerSpec
from .health import Health

VENV = Path(sys.executable).parent
MCP_PROXY_PORT_BASE = 9400


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ManagedServer:
    """stdio-сервер: живёт как ребёнок mcp-proxy на своём порту."""

    def __init__(self, spec: ServerSpec, health: Health):
        self.spec = spec
        self.health = health
        self.proc: subprocess.Popen | None = None
        self.proxy_port: int | None = None

    def start(self, cfg: Config) -> None:
        if self.spec.kind != "stdio" or (self.proc and self.proc.poll() is None):
            return
        env = {**os.environ, **self.spec.env}
        egress = cfg.egress_for(self.spec)
        if egress:
            env.setdefault("HTTPS_PROXY", egress)
            env.setdefault("HTTP_PROXY", egress)
            env.setdefault("ALL_PROXY", egress)
        env["NO_PROXY"] = self.spec.env.get(
            "NO_PROXY", "127.0.0.1,localhost")
        self.proxy_port = _free_port()
        try:
            if getattr(sys, "frozen", False):
                # exe: mcp-proxy кладём рядом с AOMG.exe отдельным exe
                proxy_cmd = [str(Path(sys.executable).parent
                                 / "mcp-proxy.exe")]
            else:
                proxy_cmd = [str(VENV / "python.exe"), "-m", "mcp_proxy"]
            self.proc = subprocess.Popen(
                [*proxy_cmd,
                 "--port", str(self.proxy_port), "--host", "127.0.0.1",
                 "--pass-environment",
                 self.spec.command, *self.spec.args],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, env=env,
                creationflags=subprocess.CREATE_NO_WINDOW)
            self.health.pid = self.proc.pid
        except FileNotFoundError as e:
            self.health.record("down", error=str(e), ts=time.time())

    def alive(self) -> bool:
        if self.spec.kind != "stdio":
            return True
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class Supervisor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.managed: dict[str, ManagedServer] = {}
        self._lock = threading.Lock()

    def start_all(self) -> None:
        for name, spec in self.cfg.servers.items():
            h = Health()
            self.managed[name] = ManagedServer(spec, h)
            if spec.kind == "stdio":
                self.managed[name].start(self.cfg)

    def ensure(self, name: str) -> None:
        """Создать ManagedServer после добавления в cfg (config уже обновлён)."""
        with self._lock:
            spec = self.cfg.servers.get(name)
            if spec and name not in self.managed:
                h = Health()
                self.managed[name] = ManagedServer(spec, h)
                if spec.kind == "stdio":
                    self.managed[name].start(self.cfg)

    def remove(self, name: str) -> None:
        with self._lock:
            m = self.managed.pop(name, None)
            if m:
                m.stop()

    def restart(self, name: str) -> None:
        with self._lock:
            m = self.managed.get(name)
            if m and m.spec.kind == "stdio":
                m.stop()
                m.start(self.cfg)

    def stop_all(self) -> None:
        for m in self.managed.values():
            m.stop()

    def upstream_for(self, name: str) -> str | None:
        m = self.managed.get(name)
        if m and m.spec.kind == "stdio":
            if m.proxy_port and m.alive():
                return f"http://127.0.0.1:{m.proxy_port}/mcp/"
            return None
        spec = self.cfg.servers.get(name)
        return spec.url if spec else None
