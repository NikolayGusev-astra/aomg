"""Трей-иконка: картинка приложения + цветовой индикатор, меню статуса/действий.

v0.2: трей открывает админку в браузере, «Перезапустить всё», «Выход».
--no-tray пропускает трей (CI/e2e).
"""
from __future__ import annotations

import os
import sys
import threading
import webbrowser
from pathlib import Path

from PIL import Image, ImageDraw

from .health import aggregate_state

COLORS = {"ok": (74, 222, 128), "reconnecting": (251, 191, 36),
          "channel_down": (251, 191, 36), "down": (248, 113, 113)}

_STATE_TEXT = {"ok": "работает", "reconnecting": "перезапуск…",
               "channel_down": "нет сети", "down": "не отвечает"}


def _app_icon() -> Image.Image | None:
    """Иконка приложения: рядом с exe (frozen) / в ресурсах / в репо."""
    candidates = []
    if getattr(sys, "frozen", False):
        me = Path(sys.executable).parent
        candidates.append(me / "app-icon.png")
        candidates.append(me / "_internal" / "assets" / "app-icon.png")
        candidates.append(me / "assets" / "app-icon.png")
    here = Path(__file__).resolve().parent.parent
    candidates.append(here / "assets" / "app-icon.png")
    for p in candidates:
        if p.exists():
            try:
                return Image.open(p).convert("RGBA")
            except Exception:
                continue
    return None


def make_icon(state: str, base: Image.Image | None = None) -> Image.Image:
    """Джилл + зелёный/жёлтый/красный индикатор в правом нижнем углу."""
    if base is None:
        base = _app_icon()
    if base is None:
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse([4, 4, 60, 60], fill=(167, 139, 250, 255))
        img = img.resize((64, 64))
    else:
        img = base.copy()
        # кроп по центру до квадрата, чтобы не сплющить широкий арт
        w, h = img.size
        side = min(w, h)
        img = img.crop(((w - side) // 2, (h - side) // 2,
                        (w + side) // 2, (h + side) // 2))
        img = img.resize((64, 64), Image.LANCZOS)
    dot = COLORS.get(state, COLORS["down"])
    d = ImageDraw.Draw(img)
    w, h = img.size
    # Статус-точка увеличена: в трее иконка рендерится ~16px
    cx, cy = int(w * 0.76), int(h * 0.78)
    r = max(9, w // 6)
    d.ellipse([cx - r - 2, cy - r - 2, cx + r + 2, cy + r + 2],
              fill=(20, 18, 31, 255))
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(*dot, 255))
    return img


def run_tray(healths, supervisor, on_quit, gateway_port: int = 9300) -> None:
    import pystray

    def open_admin(*_):
        webbrowser.open(f"http://127.0.0.1:{gateway_port}/admin")

    def restart_all(*_):
        for n in list(supervisor.managed):
            supervisor.restart(n)

    menu = pystray.Menu(
        pystray.MenuItem(lambda _: status_line(healths), None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Открыть управление", open_admin, default=True),
        pystray.MenuItem("Перезапустить всё", restart_all),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Выход", lambda *_: on_quit()))

    base_icon = _app_icon()
    icon = pystray.Icon("AOMG", make_icon("reconnecting", base_icon),
                        "AOMG — MCP-шлюз", menu)

    def refresher():
        import time
        while True:
            state = aggregate_state(list(healths.values()))
            try:
                icon.icon = make_icon(state, base_icon)
                icon.title = "AOMG — " + status_line(healths)
            except Exception:
                pass
            time.sleep(5)

    threading.Thread(target=refresher, daemon=True).start()
    icon.run()


def status_line(healths) -> str:
    parts = [f"{n}: {h.state}" + (f" ({h.tools} тулов)" if h.state == "ok" else "")
             for n, h in healths.items()]
    return " | ".join(parts) or "нет серверов"
