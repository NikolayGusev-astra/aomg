"""Точка входа для frozen mcp-proxy: запускает пакетный __main__ как модуль."""
import runpy
import sys

sys.argv[0] = "mcp-proxy"

# mcp_proxy/__main__.py использует относительные импорты -> нужен package context
import mcp_proxy.__main__  # noqa: E402  (исполняет модуль, __name__ != __main__)

# но его main() под if __name__ == "__main__" не вызовется — зовём явно
runpy.run_module("mcp_proxy.__main__", run_name="__main__")
