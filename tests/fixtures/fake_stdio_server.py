"""Фейковый stdio MCP-сервер для e2e: чистый JSON-RPC, без зависимостей.

Говорит по протоколу 2024-11-05, одна тулка fake_echo.
"""
import json
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def reply(msg_id, result):
    print(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}),
          flush=True)


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method = msg.get("method", "")
        msg_id = msg.get("id")
        if method == "initialize":
            reply(msg_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "0.0.1"}})
        elif method == "tools/list":
            reply(msg_id, {"tools": [{
                "name": "fake_echo",
                "description": "Echo back the input text",
                "inputSchema": {"type": "object",
                                "properties": {"text": {"type": "string"}},
                                "required": ["text"]}}]})
        elif method == "tools/call":
            args = msg.get("params", {}).get("arguments", {})
            reply(msg_id, {"content": [
                {"type": "text", "text": args.get("text", "")}]})


if __name__ == "__main__":
    main()
