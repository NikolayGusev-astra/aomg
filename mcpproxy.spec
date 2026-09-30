# -*- mode: python ; coding: utf-8 -*-
# Отдельный onedir для mcp-proxy (stdio-дети в frozen-режиме).
# Onedir: mcp-proxy.exe кладём в dist/AOMG/ рядом с AOMG.exe —
# supervisor ищет его по Path(sys.executable).parent.
from PyInstaller.utils.hooks import copy_metadata

# mcp.server.fastmcp зовёт importlib.metadata.version("mcp") — без
# dist-info frozen mcp-proxy падает PackageNotFoundError на старте
datas = copy_metadata("mcp") + copy_metadata("mcp_proxy")

a = Analysis(
    ["mpx-entry.py"],
    pathex=["."],  # mcp-proxy должен быть установлен в тот же venv/python
    binaries=[], datas=datas,
    hiddenimports=["mcp_proxy", "mcp_proxy.__main__", "mcp"],
    hookspath=[], hooksconfig={}, runtime_hook=[],
    excludes=[], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [],
          exclude_binaries=True,
          name="mcp-proxy", console=False, upx=False,
          # СВОЯ папка рантайма: если бы звалась "_internal", mcp-proxy.exe,
          # положенный рядом с AOMG.exe, подхватил бы ЧУЖУЮ _internal от AOMG
          # (без метаданных mcp) и падал PackageNotFoundError каждые 30с.
          # COLLECT берёт contents_directory из EXE.
          contents_directory="mcp_proxy_internal")
coll = COLLECT(exe, a.binaries, a.datas,
               name="mcp-proxy")
