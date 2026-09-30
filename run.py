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
from aomg.health import Health
from aomg.supervisor import Supervisor
from aomg.watchdog import start_watchdog


def _app_dir() -> Path:
    """Папка установки. В frozen (PyInstaller onefile) __file__ указывает
    на временную папку распаковки _MEIxxxx — конфиг ищем рядом с самим exe,
    как это делают иконка ярлыка и пользователь."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


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
    cfg = load_config(cfg_path)
    healths = {n: Health() for n in cfg.servers}

    sup = Supervisor(cfg)
    sup.start_all()

    from aomg.admin import register_admin

    def configure(app):
        register_admin(app, cfg, sup, healths, cfg_path,
                       restart_watchdog=lambda: None)

    gw_thread = serve_in_thread(cfg, sup, healths, configure=configure)
    start_watchdog(cfg, sup, healths, interval=30.0)

    # прогрев каталога: индекс реестра должен быть свежим к открытию админки
    from aomg.index import CatalogIndex, warm
    warm(CatalogIndex(cfg_path.parent / "registry-index.json"))

    if args.no_tray:
        while gw_thread.is_alive():
            time.sleep(1)
        return 0

    from aomg.tray import run_tray

    def on_quit():
        sup.stop_all()
        os._exit(0)

    signal.signal(signal.SIGTERM, lambda *_: on_quit())
    run_tray(healths, sup, on_quit, gateway_port=cfg.gateway_port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
