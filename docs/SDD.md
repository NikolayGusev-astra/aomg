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
aomg/config.py    загрузка/сохранение config.yaml, atomic write;
                  ${VAR} подставляется из process-env и HKCU\Environment
aomg/supervisor.py  спавн stdio-детей (порт-прокси pair, "--" перед командой),
                  рестарт, pin-версии; без egress прокси-переменные хоста
                  из окружения ребёнка удаляются
aomg/gateway.py   FastAPI: /<name>/mcp -> прокси к ребенку или апстриму
aomg/watchdog.py  периодический initialize+tools/list через egress группы
aomg/registry.py  клиент registry.modelcontextprotocol.io (/v0/servers)
aomg/index.py     локальный индекс каталога: полный слив реестра на диск
                  (cursor-пагинация, дедуп по короткому имени), мгновенный
                  поиск по индексу, stale-while-revalidate обновление в фоне;
                  упавший реестр не смывает индекс
aomg/catalog.py   поиск с TTL-кэшем 24ч (при недоступном реестре отдаётся
                  устаревший кэш)
aomg/admin.py     веб-панель: статусы, каталог (поиск по индексу из
                  aomg/index.py, живой фолбэк пока индекса нет), форма
                  ключей, рестарт; добавление сервера перечитывает
                  рантайм-конфиг до спавна
aomg/client_sync.py  прописывание url-записей в конфиг MCP-клиента
aomg/tray.py      pystray: цвет по Health-агрегату, меню, "Перезапустить всё"
```

## Критерии (BDD, проверяемые)

1. stdio-сервер из конфига спавнится; tools/list через гейтвей отдаёт его тулы.
2. Убить ребёнка извне → супервизор перезапускает; следующий tools/list OK.
3. HTTP-апстрим через группу с прокси: запрос уходит с proxy-клиентом группы.
4. Апстрим недоступен, egress-канал недоступен → state=channel_down (не down).
5. Апстрим отвечает 500/битым JSON-RPC → state=down.
6. client_sync: после запуска в конфиге клиента есть url-запись; idempotent.
7. Registry: search отдаёт записи; для записей с headers → форма содержит
   поле секрета; для packages → kind=stdio + pin версии.
7a. Каталог: поиск по локальному индексу отвечает < 1 c при любом качестве
    сети; индекс старше 24 ч обновляется в фоне, не блокируя ответ; падение
    реестра не удаляет индекс.
8. Watchdog пишет историю Health; иконка = worst-of (down красная,
   channel_down/reconnecting жёлтая, ok зелёная).

## Не в v0.1

Алерты в TG, fallback-цепочки в рантайме (задаётся в конфиге, но логика проб
только primary), удалённый доступ к гейтвею (только 127.0.0.1), порт на Rust.

## Известные грабли (не повторять)

- mcp-proxy 0.9.0 пин: 0.10+ сломаны для сценария stdio-проксирования.
- Маршрут ребёнка внутри mcp-proxy — `/mcp/` с trailing slash.
- В спавне обязателен `--` перед командой: иначе `npx -y pkg` теряет `-y`
  (парсится как флаг mcp-proxy), ребёнок циклически перезапускается.
- Дети без egress не должны наследовать HTTPS_PROXY/HTTP_PROXY хоста:
  httpx-клиенты игнорируют `NO_PROXY="*"`.
- `${VAR}` секреты могут жить только в User-окружении Windows
  (HKCU\Environment) — process-env их не содержит.
