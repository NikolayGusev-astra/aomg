# -*- mode: python ; coding: utf-8 -*-
# AOMG PyInstaller spec: onefile windowed exe с иконкой.
import os

a = Analysis(
    ["run.py"],
    pathex=["."],
    binaries=[],
    datas=[("assets/app-icon.png", "assets")],
    hiddenimports=[
        "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto",
        "uvicorn.protocols", "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto", "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto", "uvicorn.lifespan",
        "uvicorn.lifespan.on", "anyio._backends._asyncio",
        "pystray._win32",
    ],
    hookspath=[], hooksconfig={}, runtime_hook=[],
    excludes=[], noarchive=False)
pyz = PYZ(a.pure)

exe = EXE(pyz, a.scripts, a.binaries, a.datas,
          name="AOMG",
          icon="assets/app-icon.ico" if os.path.exists(
              "assets/app-icon.ico") else None,
          console=False,            # трей-приложение: без консоли
          disable_windowed_traceback=False,
          upx=False)
