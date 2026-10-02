"""AOMG entry point.

run.py [--no-tray] [--config PATH]
Env: AOMG_CONFIG (default: ./config.yaml рядом с run.py)
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from aomg.config import load_config
from aomg.gateway import create_app, serve_in_thread
from aomg.port import PortUnavailable, resolve_gateway_port
from aomg.supervisor import Supervisor
from aomg.watchdog import start_watchdog

DEFAULT_PORT = 9300


def _app_dir() -> Path:
    """Папка установки. В frozen (PyInstaller onefile) __file__ указывает
    на временную папку распаковки _MEIxxxx — конфиг ищем рядом с самим exe,
    как это делают иконка ярлыка и пользователь."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def write_default_config(cfg_path: Path, gateway_port: int) -> None:
    """Первый запуск: чистый конфиг с ПРОВЕРЕННЫМ портом.

    Демо-серверов здесь нет намеренно: пример, который не запускается
    на машине юзера, превращается в рестарт-цикл (ADR-0004/0006).
    Порт сюда пишется фактический — тот, что resolve_gateway_port
    проверил на свободный, а не константа 9300.
    """
    cfg_path.write_text(
        "# AOMG config — серверы добавляются через панель\n"
        f"# http://127.0.0.1:{gateway_port}/admin\n"
        f"gateway_port: {gateway_port}\n\n"
        "groups:\n"
        '  direct: {name: "Напрямую", proxy: null}\n',
        encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(prog="AOMG")
    ap.add_argument("--no-tray", action="store_true")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    # Запуск двойным кликом: sys.stdout/stderr == None (нет консоли),
    # uvicorn-форматтер падает на .isatty() при настройке логов.
    # Перенаправляем в файл (или devnull) ДО инициализации uvicorn.
    if getattr(sys, "frozen", False) and sys.stdout is None:
        log_dir = _app_dir() / "logs"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            sys.stdout = open(log_dir / "aomg.log", "a",
                              encoding="utf-8", errors="replace")
        except OSError:
            sys.stdout = open(os.devnull, "w")
        sys.stderr = sys.stdout

    cfg_path = (Path(args.config) if args.config else Path(
        os.environ.get("AOMG_CONFIG",
                       _app_dir() / "config.yaml")))

    # Порт гейтвея (ADR-0006). Различаем два случая: порт НЕ задан
    # пользователем — подставляем свободный и пишем факт; задан и
    # занят — диагностика и код выхода 2. Молча уводить гейтвей на
    # другой адрес нельзя: агент настроен на этот URL из конфига.
    port = DEFAULT_PORT
    explicit_port = False
    if cfg_path.exists():
        try:
            import yaml
            raw = yaml.safe_load(cfg_path.read_text(
                encoding="utf-8")) or {}
            if raw.get("gateway_port"):
                port = int(raw["gateway_port"])
                explicit_port = True
        except Exception:
            explicit_port = False
    try:
        port = resolve_gateway_port(port, explicit=explicit_port)
    except PortUnavailable as e:
        print(f"[aomg] {e}", flush=True)
        return 2
    if not explicit_port:
        print(f"[aomg] gateway port {port}", flush=True)

    if not cfg_path.exists():
        # Первый запуск: чистый конфиг БЕЗ демо-серверов. Пример из
        # документации, который не запускается на машине юзера, хуже
        # отсутствия примера: AOMG сам поднимает и рестартует stdio-детей,
        # поэтому мёртвый демо превращается в вечный рестарт-цикл.
        write_default_config(cfg_path, gateway_port=port)
        print(f"[aomg] created default config: {cfg_path}", flush=True)

    cfg = load_config(cfg_path)
    cfg.gateway_port = port

    # Health заводит Supervisor, а не run.py: раньше словарь создавался
    # здесь, а сервер, добавленный в панели, в него не попадал —
    # и watchdog падал на KeyError (ADR-0003).
    sup = Supervisor(cfg)
    sup.start_all()
    sup.collect_secrets()

    from aomg.admin import register_admin

    # Индекс реестра и его фоновый автосинк. Раньше здесь был разовый
    # warm(), который глотал исключения и запускал полный обход реестра
    # (~270 с на 2794 записи) на старте - панель всё это время
    # показывала official пустым, и это приходилось объяснять как
    # «честное окно». Теперь sync живёт в фоновом потоке, а панель
    # читает его статус (ADR-0005).
    from aomg.index import CatalogIndex, IndexSync

    catalog_index = CatalogIndex(cfg_path.parent / "registry-index.json")
    index_sync = IndexSync(
        catalog_index,
        interval=float(os.environ.get("AOMG_SYNC_INTERVAL") or 6 * 3600.0),
        enabled=os.environ.get("AOMG_NO_AUTOSYNC") != "1")
    index_sync.start()

    def configure(app):
        register_admin(app, cfg, sup, cfg_path,
                       restart_watchdog=lambda: None,
                       index=index_sync)

    gw_thread = serve_in_thread(cfg, sup, configure=configure)
    # Интервал watchdog настраивается: e2e обязан проверять рестарт
    # ребёнка за секунды, а не за полминуты (ADR-0004).
    interval = float(os.environ.get("AOMG_WATCH_INTERVAL") or 30.0)
    start_watchdog(cfg, sup, interval=interval)

    if args.no_tray:
        while gw_thread.is_alive():
            time.sleep(1)
        return 0

    from aomg.tray import run_tray

    def on_quit():
        sup.stop_all()
        os._exit(0)

    signal.signal(signal.SIGTERM, lambda *_: on_quit())
    run_tray(sup, on_quit, gateway_port=cfg.gateway_port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
