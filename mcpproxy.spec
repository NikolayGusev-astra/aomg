# -*- mode: python ; coding: utf-8 -*-
# Отдельный onefile для mcp-proxy (stdio-дети в frozen-режиме).
a = Analysis(
    ["mpx-entry.py"],
    pathex=["."],  # mcp-proxy должен быть установлен в тот же venv/python
    binaries=[], datas=[],
    hiddenimports=["mcp_proxy", "mcp_proxy.__main__", "mcp"],
    hookspath=[], hooksconfig={}, runtime_hook=[],
    excludes=[], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas,
          name="mcp-proxy", console=False, upx=False)
