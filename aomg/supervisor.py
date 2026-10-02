"""Супервизор: stdio-дети через mcp-proxy + ВЛАДЕНИЕ рантайм-состоянием.

Два решения живут здесь, оба из ADR:

* **ADR-0003 (runtime-реестр).** `Supervisor` — единственный владелец
  `ManagedServer` и `Health`. Словарь `healths` больше не передаётся
  четырём модулям как параметр: добавить сервер «наполовину» нельзя,
  потому что единственная точка мутации — `add`/`remove` под локом.

* **ADR-0004 (политика перезапуска).** Смерть ребёнка ≠ повод для
  бесконечного респауна. Здесь живут backoff и circuit breaker; watchdog
  только сообщает факт. Корневая причина респауна из лога установленной
  сборки — `mcp 2.x` вместо `mcp<2` в пакете ребёнка.

Логи: stdout/stderr каждого mcp-proxy (и ребёнка) пишутся в
<logs_dir>/<name>.log (ротация: при превышении LOG_MAX байтов файл
переименовывается в <name>.log.old). logs_dir: <app_dir>/logs
(frozen) или ./logs; путь настраивается через set_logs_dir().
Перед отдачей в API лог проходит redact_secrets() — ключи из конфига
не должны утекать в /health (ADR-0006).
"""
from __future__ import annotations

import os
import re
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
REDACTED = "***"

# ADR-0004: паузы между авторестартами, секунды. Первый — сразу (ребёнка
# убили извне, пользователь не должен ждать), дальше с потолком 60 с.
RESTART_BACKOFF = (0, 5, 15, 30, 60)
MAX_FAILS = 3                  # после — разомкнуть автомат, не рестартить

_logs_dir: Path | None = None

# Счётчик спавнов — только для тестов (ADR-0004 I1): production-код его
# не читает, тесты проверяют конечность рестарт-цикла.
spawn_counter: list = []


def set_logs_dir(path: Path | None) -> None:
    global _logs_dir
    if path is None:
        _logs_dir = None
        return
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


_SECRET_PLACEHOLDER = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")

# Неразвёрнутый ${VAR} ВНУТРИ значения (в т.ч. составного: "Bearer ${T}").
_UNRESOLVED_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def unresolved_vars(spec: ServerSpec) -> list[str]:
    """${VAR}, оставшиеся литералом после load_config.

    load_config._expand разворачивает плейсхолдеры из process-env и
    HKCU\\Environment; если переменной нет ни там ни там, значение
    доходит до спавна как литерал. Ребёнок получит мусорный ключ и
    умрёт на initialize с невнятным auth-failed вместо честного
    «переменная не задана». Ловим до спавна — по тем же причинам, что
    и command_missing (один down с причиной вместо рестарт-цикла).
    """
    names: set[str] = set()
    for src in (spec.env, spec.headers):
        for v in src.values():
            if isinstance(v, str):
                names.update(_UNRESOLVED_VAR.findall(v))
    return sorted(names)


def collect_secret_values(env: dict, headers: dict,
                          extra: list | None = None) -> list[str]:
    """Значения, которые нельзя показывать в логах и API.

    Неразвёрнутые `${VAR}` плейсхолдеры исключены: иначе redactor
    затёр бы текст вида «переменная ${GITHUB_PAT} не задана».
    """
    out: list[str] = []
    for src in (env or {}, headers or {}):
        for v in src.values():
            if isinstance(v, str) and len(v) >= 6 \
                    and not _SECRET_PLACEHOLDER.match(v.strip()):
                out.append(v)
    for v in extra or []:
        if isinstance(v, str) and len(v) >= 6:
            out.append(v)
    return out


def redact_secrets(text: str | None, secrets: list | None) -> str:
    """Вырезать значения секретов из произвольного текста (лог, URL)."""
    if not text:
        return ""
    out = text
    for s in secrets or []:
        if s and len(s) >= 6:
            out = out.replace(s, REDACTED)
    return out


def last_error(name: str, secrets: list | None = None) -> str | None:
    """Хвост лога сервера — для /health и панели, с маскированием."""
    for p in (logs_dir() / f"{name}.log",
              logs_dir() / f"{name}.log.old"):
        try:
            if p.exists():
                text = p.read_text(encoding="utf-8", errors="replace")
                lines = [l for l in text.strip().splitlines() if l.strip()]
                if lines:
                    tail = redact_secrets("\n".join(lines[-5:]), secrets)
                    return tail[:LOG_TAIL]
        except OSError:
            continue
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def which_command(cmd: str) -> str | None:
    """Аналог shutil.which, умеющий .exe/.bat/.cmd (Windows)."""
    from shutil import which
    found = which(cmd)
    if found:
        return found
    for ext in (".exe", ".bat", ".cmd", ".com"):
        found = which(cmd + ext)
        if found:
            return found
    return None


def command_missing(spec: ServerSpec) -> bool:
    """Команда не найдена ни в PATH, ни как существующий путь.

    Ловит класс отказа «бинаря нет» до спавна: иначе mcp-proxy
    стартует, падает и порождает рестарт-цикл ради заведомо мёртвой
    команды. Ответ ложный для npm/uvx-обёрток (npx, uvx, node, python) —
    они резолвятся в shell, поэтому проверяем только исполняемые имена.
    """
    cmd = (spec.command or "").strip()
    if not cmd:
        return True
    if any(sep in cmd for sep in ("\\", "/")):
        return not Path(cmd).exists()
    return which_command(cmd) is None


class ManagedServer:
    """stdio-сервер: живёт как ребёнок mcp-proxy на своём порту."""

    def __init__(self, spec: ServerSpec, health: Health):
        self.spec = spec
        self.health = health
        self.proc: subprocess.Popen | None = None
        self.proxy_port: int | None = None
        self._log_fh = None
        # Спавн заблокирован предварительной проверкой (команда не найдена,
        # секрет не развёрнут): down с причиной поставлен один раз, watchdog
        # не должен гнать рестарт-цикл по заведомо неживому ребёнку
        # (ADR-0004: конечность Popen). Снимается только stop() при
        # смене спеки/ручном рестарте.
        self.blocked: str | None = None

    def start(self, cfg: Config) -> None:
        if self.spec.kind != "stdio" or (self.proc and self.proc.poll() is None):
            return
        if command_missing(self.spec):
            # Не спавним заведомо мёртвую команду: один раз — и down с
            # внятной причиной, а не рестарт-цикл (ADR-0004).
            self.blocked = f"команда не найдена: {self.spec.command}"
            self.health.record(
                "down",
                error=self.blocked,
                ts=time.time())
            return
        missing = unresolved_vars(self.spec)
        if missing:
            # Не спавним ребёнка с неразвёрнутым секретом: он получит
            # литерал ${VAR} и умрёт на initialize с ложным
            # «auth failed». Один down с именами переменных вместо
            # рестарт-цикла (тот же контракт, что и command_missing).
            self.blocked = ("переменные окружения не заданы: "
                            + ", ".join(f"${{{n}}}" for n in missing))
            self.health.record("down", error=self.blocked, ts=time.time())
            return
        self.blocked = None
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
            spawn_counter.append(self.spec.name)
            self.spawned_at = time.time()
            self.health.pid = self.proc.pid
            self.health.record("reconnecting", ts=time.time())
        except FileNotFoundError as e:
            self.health.record("down", error=str(e), ts=time.time())

    def alive(self) -> bool:
        if self.spec.kind != "stdio":
            return True
        if self.blocked:
            # Ребёнок заведомо не спавнился: для политики рестарта он
            # не «умер» — его вообще нет. alive=True держит watchdog
            # от record_failure-цикла по вечному down.
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
    """Владелец рантайм-состояния: процессы + Health + рестарт-политика.

    ADR-0003: `add`/`remove` — единственные точки мутации, обе под
    `_lock`. Всё остальное читает через `health()`/`snapshot()`, поэтому
    отсутствие записи — это `None`, а не KeyError.

    ADR-0004: `watch_tick()` — один шаг политики для одного сервера.
    Рестарт не бесконечен: `MAX_FAILS` попыток, потом `down` с причиной.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.managed: dict[str, ManagedServer] = {}
        self.healths: dict[str, Health] = {}
        self.fails: dict[str, int] = {}
        self.next_allowed: dict[str, float] = {}
        self.secrets: list[str] = []
        self._lock = threading.Lock()

    # ---- секреты (ADR-0006: маскирование в API) ----

    def collect_secrets(self) -> list[str]:
        out: list[str] = []
        for spec in self.cfg.servers.values():
            out += collect_secret_values(spec.env, spec.headers)
        self.secrets = out
        return out

    def set_secrets(self, values) -> None:
        self.secrets = [v for v in values if isinstance(v, str) and v]

    def last_error(self, name: str) -> str | None:
        """Хвост лога с маскированием секретов (не модульная функция:
        модульная last_error() требует явного списка секретов)."""
        return last_error(name, self.secrets)

    # ---- мутации: единственные точки (ADR-0003 I1) ----

    def add(self, name: str, spec: ServerSpec | None = None) -> Health:
        """Создать/обновить сервер: cfg.servers + Health + ManagedServer
        атомарно, под одним локом (ADR-0003 I1)."""
        spec = spec if spec is not None else self.cfg.servers.get(name)
        if spec is None:
            raise KeyError(name)
        with self._lock:
            self.cfg.servers[name] = spec
            h = self.healths.get(name)
            if h is None:
                h = self.healths[name] = Health()
            m = self.managed.get(name)
            if m is None:
                m = self.managed[name] = ManagedServer(spec, h)
            else:
                m.spec = spec
                m.health = h
        if spec.kind == "stdio":
            m.start(self.cfg)
        return h

    def remove(self, name: str) -> None:
        """Снять сервер из рантайма и из конфига (панель «Удалить»)."""
        with self._lock:
            m = self.managed.pop(name, None)
            if m:
                m.stop()
            self.healths.pop(name, None)
            self.fails.pop(name, None)
            self.next_allowed.pop(name, None)
            self.cfg.servers.pop(name, None)

    def restart(self, name: str, manual: bool = False) -> None:
        """Перезапуск. manual=True — кнопка панели: сбрасывает цепь
        неудач и замыкает разомкнутый автомат (ADR-0004 I3)."""
        with self._lock:
            m = self.managed.get(name)
            if m is None or m.spec.kind != "stdio":
                return
            if manual:
                self.fails[name] = 0
                self.next_allowed[name] = 0.0
            m.stop()
            m.blocked = None   # ручной рестарт снимает блокировку спавна
        m.start(self.cfg)
        if manual:
            if m.blocked:
                # start() снова заблокировал и поставил честный down —
                # не затираем его «перезапуском вручную»
                return
            m.health.record("reconnecting",
                            error="перезапуск вручную", ts=time.time())

    def start_all(self) -> None:
        for name in list(self.cfg.servers):
            self.add(name)

    def ensure(self, name: str) -> Health | None:
        """Создать, если ещё нет; иначе вернуть существующий Health."""
        if name in self.managed:
            return self.healths.get(name)
        if name not in self.cfg.servers:
            return None
        return self.add(name)

    def stop_all(self) -> None:
        for name in list(self.managed):
            m = self.managed.get(name)
            if m:
                m.stop()

    # ---- чтение (ADR-0003 I2: без KeyError) ----

    def health(self, name: str) -> Health | None:
        return self.healths.get(name)

    def snapshot(self) -> list:
        """Согласованный срез (name, Health) под локом, в порядке конфига.

        `list(...)` обязателен: пока панель добавляет сервер, обход
        внешнего словаря без копии даёт `RuntimeError: dictionary changed
        size during iteration` — watchdog на этом молча умирал.
        """
        with self._lock:
            servers = dict(self.cfg.servers)
            healths = dict(self.healths)
        names = [n for n in servers if n in healths]
        extra = [n for n in healths if n not in servers]
        return [(n, healths[n]) for n in names + extra]

    def upstream_for(self, name: str) -> str | None:
        m = self.managed.get(name)
        if m and m.spec.kind == "stdio":
            if m.proxy_port and m.alive():
                return f"http://127.0.0.1:{m.proxy_port}/mcp/"
            return None
        spec = self.cfg.servers.get(name)
        return spec.url if spec else None

    # ---- политика перезапуска (ADR-0004) ----

    def backoff_for(self, name: str) -> float:
        """Пауза перед N-й попыткой рестарта (N = число неудач)."""
        n = self.fails.get(name, 0)
        return float(RESTART_BACKOFF[min(max(n, 0), len(RESTART_BACKOFF) - 1)])

    def note_alive(self, name: str, ts: float | None = None) -> None:
        """Сервер ответил — цепь неудач обнуляется и статус становится
        `ok` (ADR-0004 I2). Вызывается ровно тогда, когда проба дала ok."""
        self.fails[name] = 0
        self.next_allowed[name] = 0.0
        h = self.healths.get(name)
        if h is not None:
            h.record("ok", ts=ts if ts is not None else time.time())

    def record_failure(self, name: str, ts: float,
                       reason: str | None = None) -> str:
        """Один шаг политики для упавшего stdio-сервера.

        Возвращает новое состояние. Рестарт — не более MAX_FAILS раз,
        дальше автомат разомкнут: состояние `down` с причиной из лога,
        ручной рестарт из панели возвращает сервер в работу.
        """
        h = self.healths.get(name)
        if h is None:
            return "unknown"
        n = self.fails.get(name, 0) + 1
        self.fails[name] = n
        tail = reason or self.last_error(name) or ""
        tail = " ".join(tail.split())[:200]

        if n > MAX_FAILS:
            h.record("down", ts=ts,
                     error=f"авторестарт остановлен после {MAX_FAILS} попыток. "
                           f"Причина: {tail or 'см. лог сервера'}")
            return "down"

        if ts < self.next_allowed.get(name, 0.0):
            h.record("reconnecting", ts=ts,
                     error=f"пауза перед рестартом "
                           f"{self.next_allowed[name] - ts:.0f} с")
            return "reconnecting"

        self.next_allowed[name] = ts + self.backoff_for(name)
        self.restart(name)
        if n >= MAX_FAILS:
            h.record("down", ts=ts,
                     error=f"{n} попыток, авторестарт прекращён. "
                           f"Причина: {tail or 'см. лог сервера'}")
            return "down"
        h.record("reconnecting", ts=ts, error=tail or "ребёнок умер, рестарт")
        return "reconnecting"

    def watch_tick(self, ts: float, probe=None) -> None:
        """Один проход политики по всем серверам.

        `probe(name) -> (state, tools, error)`; None — только контроль
        процессов (используется в тестах и в первом проходе). Сервер
        без Health пропускается, а не роняет проход: рассинхрон конфига
        не должен убивать watchdog (ADR-0003).
        """
        # копия списка: панель может добавить сервер прямо во время
        # прохода, и обход dict без копии даёт RuntimeError
        for name, spec in list(self.cfg.servers.items()):
            h = self.healths.get(name)
            if h is None:
                continue
            m = self.managed.get(name)
            if spec.kind == "stdio" and m is not None and not m.alive():
                self.record_failure(name, ts, None)
                continue
            if probe is None:
                continue
            state, tools, err = probe(name)
            if state == "ok":
                self.note_alive(name)
            elif m is not None and m.blocked:
                # Сервер заблокирован предварительной проверкой: проба по
                # несуществующему порту даёт channel_down и затёрла бы
                # честную причину блокировки. Причину сохраняем.
                state, err = "down", m.blocked
            h.record(state, tools=tools, error=err, ts=ts)
