# SDD: AOMG v0.1

## Цель

Трей-приложение Windows: держит MCP живыми, отдаёт агентам по одному HTTP-порту,
различает типы отказа, позволяет не-инженеру подключать новые MCP из каталога.

## Домен (типы первичны)

```python
Egress = None | str                                  # None=direct, str=proxy URL
Group  = {name: str, proxy: Egress, check: str|None} # check = precondition probe
ServerSpec = {
  name: str,
  kind: "stdio" | "http",        # доставка: свой ребёнок или чужой URL
  command/args/env  (stdio)  |  url/headers  (http),
  group: str,                    # индекс в groups
  fallback: list[str],           # порядок проб egress
  pinned_version: str | None,    # для stdio из реестра
}
Health = {state: "ok"|"reconnecting"|"down"|"channel_down",
          tools: int, last_check: ts, error: str|None}
```

Инвариант: kind выводится из конфига (url есть → http; command есть → stdio),
не хранится отдельно. Health.state — единственный источник цвета иконки.

## Компоненты

```
run.py            точка входа: config load, uvicorn-поток, pystray в main
aomg/config.py    загрузка/сохранение config.yaml, atomic write
aomg/supervisor.py  спавн stdio-детей (порт-прокси pair), рестарт, pin-версии
aomg/gateway.py   FastAPI: /<name>/mcp -> прокси к ребенку или апстриму
aomg/watchdog.py  периодический initialize+tools/list через egress группы
aomg/registry.py  клиент registry.modelcontextprotocol.io (/v0/servers)
aomg/client_sync.py  прописывание url-записей в конфиг MCP-клиента
aomg/tray.py      pystray: цвет по Health-агрегату, меню, "Перезапустить всё"
```

## Критерии (BDD, проверяемые)

1. stdio-сервер из конфига спавнится; tools/list через гейтвей отдаёт его тулы.
2. Убить ребёнка извне → супервизор перезапускает; следующий tools/list OK.
3. HTTP-апстрим через группу с прокси: запрос уходит с proxy-клиентом группы.
4. Апстрим недоступен, egress-канал недоступен → state=channel_down (не down).
5. Апстрим отвечает 500/битым JSON-RPC → state=down.
6. hermes_sync: после запуска в конфиге клиента есть url-запись; idempotent.
7. Registry: search отдаёт записи; для записей с headers → форма содержит
   поле секрета; для packages → kind=stdio + pin версии.
8. Watchdog пишет историю Health; иконка = worst-of (down красная,
   channel_down/reconnecting жёлтая, ok зелёная).

## Не в v0.1

GUI-каталог в окне (v0.2: CLI-команды + простое tk-окно), PyInstaller-сборка,
алерт в TG, fallback-цепочки в рантайме (задаётся в конфиге, но логика проб
только primary), удалённый доступ к гейтвею (только 127.0.0.1).
