# -*- mode: python ; coding: utf-8 -*-
# Отдельный onedir для mcp-proxy (stdio-дети в frozen-режиме).
# Onedir: mcp-proxy.exe кладём в dist/AOMG/ рядом с AOMG.exe —
# supervisor ищет его по Path(sys.executable).parent.
a = Analysis(
    ["mpx-entry.py"],
    pathex=["."],  # mcp-proxy должен быть установлен в тот же venv/python
    binaries=[], datas=[],
    hiddenimports=["mcp_proxy", "mcp_proxy.__main__", "mcp"],
    hookspath=[], hooksconfig={}, runtime_hook=[],
    excludes=[], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [],
          exclude_binaries=True,
          name="mcp-proxy", console=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas,
               name="mcp-proxy")
