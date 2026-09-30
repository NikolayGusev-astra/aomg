"""Супервизор: stdio-дети через mcp-proxy (один прокси-процесс на сервер).

Каждый stdio-сервер получает свой mcp-proxy на свободном порту 127.0.0.1;
прокси сам спавнит и держит ребёнка. Смерть ребёнка = отказ handshake —
это ловит watchdog, рестарт = перезапуск прокси.

Логи: stdout/stderr каждого mcp-proxy (и ребёнка) пишутся в файл
<logs_dir>/<name>.log (ротация: при превышении LOG_MAX байтов файл
переименовывается в <name>.log.old). logs_dir: <app_dir>/logs
(frozen) или ./logs; путь настраивается через set_logs_dir().
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

LOG_MAX = 512 * 1024          # ротация одного лог-файла
LOG_TAIL = 400                # хвост ошибки для /health
_logs_dir: Path | None = None


def set_logs_dir(path: Path) -> None:
    global _logs_dir
    _logs_dir = path
    path.mkdir(parents=True, exist_ok=True)


def logs_dir() -> Path:
    global _logs_dir
    if _logs_dir is None:
        if getattr(sys, "frozen", False):
            _logs_dir = Path(sys.executable).parent / "logs"
        else:
            _logs_dir = Path.cwd() / "logs"
        _logs_dir.mkdir(parents=True, exist_ok=True)
    return _logs_dir


def _rotate(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > LOG_MAX:
            old = path.with_suffix(".log.old")
            if old.exists():
                old.unlink()
            path.rename(old)
    except OSError:
        pass


def _open_log(name: str):
    """Открыть лог-файл сервера на дозапись (или DEVNULL при ошибке)."""
    try:
        p = logs_dir() / f"{name}.log"
        _rotate(p)
        return open(p, "a", encoding="utf-8", errors="replace")
    except OSError:
        return subprocess.DEVNULL


def last_error(name: str) -> str | None:
    """Хвост лога сервера — для /health и панели."""
    for p in (logs_dir() / f"{name}.log",
              logs_dir() / f"{name}.log.old"):
        try:
            if p.exists():
                text = p.read_text(encoding="utf-8", errors="replace")
                lines = [l for l in text.strip().splitlines() if l.strip()]
                if lines:
                    return "\n".join(lines[-5:])[:LOG_TAIL]
        except OSError:
            continue
    return None


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
        self._log_fh = None

    def start(self, cfg: Config) -> None:
        if self.spec.kind != "stdio" or (self.proc and self.proc.poll() is None):
            return
        env = {**os.environ, **self.spec.env}
        egress = cfg.egress_for(self.spec)
        if egress:
            env.setdefault("HTTPS_PROXY", egress)
            env.setdefault("HTTP_PROXY", egress)
            env.setdefault("ALL_PROXY", egress)
        else:
            # без egress дети не должны наследовать прокси хоста: exe
            # с httpx игнорирует NO_PROXY="*" и ломается о чужой socks
            for k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY",
                      "https_proxy", "http_proxy", "all_proxy"):
                env.pop(k, None)
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
            if self._log_fh:
                try:
                    self._log_fh.close()
                except OSError:
                    pass
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            self._log_fh = _open_log(self.spec.name)
            fh = self._log_fh
            try:
                fh.write(f"\n===== spawn {stamp}: "
                         f"{self.spec.command} {' '.join(self.spec.args)}\n")
                fh.flush()
            except (OSError, ValueError):
                fh = subprocess.DEVNULL
            self.proc = subprocess.Popen(
                [*proxy_cmd,
                 "--port", str(self.proxy_port), "--host", "127.0.0.1",
                 "--pass-environment",
                 "--log-level", "DEBUG",
                 # "--" обязателен: без него mcp-proxy съедает флаги
                 # команды (npx -y ... -> '-y' парсится как свой флаг,
                 # argv-ошибка, ребёнок мгновенно умирает)
                 "--",
                 self.spec.command, *self.spec.args],
                stdin=subprocess.DEVNULL, stdout=fh,
                stderr=subprocess.STDOUT, env=env,
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
        if self._log_fh:
            try:
                self._log_fh.close()
            except OSError:
                pass
            self._log_fh = None


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
