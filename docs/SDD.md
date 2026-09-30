# SDD: AOMG v0.1.2

## Цель

Трей-приложение Windows: держит MCP живыми, отдаёт агентам по одному HTTP-порту,
различает типы отказа, позволяет не-инженеру подключать новые MCP из каталога.

Обновлено 2026-09-30 по итогам аудита сессии `20260930_100646_31e2ee`.
Решения вынесены в ADR-0003…0006; ниже — следствия, а не пересказ ADR.

## Домен (типы первичны)

```python
Egress = None | str                                  # None=direct, str=proxy URL
Group  = {name: str, proxy: Egress, check: str|None} # check = precondition probe
ServerSpec = {
  name: str,
  kind: "stdio" | "http",        # доставка: свой ребёнок или чужой URL
  command/args/env  (stdio)  |  url/headers  (http),
  group: str,                    # индекс в groups
  pinned_version: str | None,    # для stdio из реестра
}
Health = {state: "ok"|"reconnecting"|"down"|"channel_down"|"unprobed",
          tools: int, last_check: ts, error: str|None, pid: int|None}
```

Инвариант: kind выводится из конфига (url есть → http; command есть → stdio),
не хранится отдельно. Health.state — единственный источник цвета иконки.

Изменения против v0.1.1:
- `fallback: list[str]` убран из ServerSpec: поле оставалось в типе, но
  логики не имело. Fallback-цепочки по-прежнему «не в v0.1».
- добавлен `pid` — без него UI и e2e не могут отличить перезапущенный
  ребёнок от того же самого процесса.
- `unprobed` — честное состояние источника до первой попытки (ADR-0005).

## Владение состоянием (ADR-0003)

`Supervisor` — единственный владелец `cfg.servers`, `managed` и `healths`.
Точки мутации: `add()`, `remove()`, `restart()`. Всё остальное читает
`health(name)` / `snapshot()`.

Отсюда следствия в коде:
- `healths` больше не передаётся в `watchdog`/`gateway`/`admin`/`tray`
  отдельным аргументом; общего словаря на две стороны нет.
- отсутствие `Health` — это `None`, а не исключение: gateway отдаёт 504,
  watchdog пропускает сервер, панель рисует «неизвестно».
- `snapshot()` копирует словари под локом: обход без копии давал
  `RuntimeError: dictionary changed size during iteration` при одновременном
  добавлении сервера из панели.
- маршрут гейтвея — один catch-all `/{name}/mcp`, а не по маршруту на
  сервер из стартового конфига: сервер, добавленный после старта, обязан
  быть доступен сразу.

## Рестарт-политика (ADR-0004)

`Supervisor.watch_tick(ts, probe=None)` — один шаг для всех серверов:

- ребёнок мёртв → `record_failure()`: пауза `RESTART_BACKOFF[n]`,
  максимум `MAX_FAILS` попыток, дальше автомат разомкнут и состояние
  `down` с причиной из лога;
- проба дала `ok` → `note_alive()`: цепь неудач обнуляется, состояние `ok`;
- `restart(name, manual=True)` из панели сбрасывает цепь и замыкает
  разомкнутый автомат — иначе кнопка «Перезапустить» ничего не даёт;
- `ManagedServer.start()` проверяет существование команды ДО спавна:
  несуществующий бинарь даёт `down` без единого `Popen`.

`watchdog.py` — наблюдатель, а не политик: он строит `probe` и передаёт
факты. Рестартов у него нет, поэтому цикл не может умереть от одного
сервера, и исключение в проходе не роняет наблюдение.

## Источники каталога (ADR-0005)

Единственный контракт источника — `search(query, limit, offset) -> {items,
total, has_more}` и `status() -> (state, error)`. Раньше у `NeuralDeepSource`
был второй несовместимый интерфейс (`list_items`/`get_item`), а панель звала
`search()` — `AttributeError` на живой сборке.

- `state` — результат последней попытки, а не константа `ok`.
- `proxy` источника приходит из его egress-группы; `group` в конфиге
  больше не декоративен.
- upsert источника пересоздаёт ТОЛЬКО его: кэш и состояние остальных
  сохраняются.
- `registry.search_async` — async с потолком 3 с (ADR-0005 I4). Синхронный
  20-секундный блок из event loop убран: панель не должна висеть.
- `CatalogIndex.load()` кэширует разбор по (mtime_ns, size, sentinel):
  на этой ФС две записи подряд получают одинаковый mtime_ns, поэтому
  одного mtime недостаточно.

## Порты и платформа (ADR-0006)

- `aomg/port.py`: `is_port_free()`, `resolve_gateway_port()`.
  Порт не задан юзером → подставляем свободный и пишем факт в конфиг.
  Порт задан и занят → `GatewayPortBusy` с текстом для человека и код
  выхода 2. Молча менять адрес нельзя: агент настроен на этот URL.
- `{port}` в шаблоне панели — единственное место, где порт рендерится.
- Релиз объявлен Windows-only. CI не публикует Linux/macOS-артефакты:
  supervisor/tray/frozen-пути содержат Windows-only предположения.
- Секреты маскируются в `/health` и `/admin/api/logs`; `collect_secrets()`
  берёт значения из env/headers серверов, неразвёрнутые `${VAR}` игнорируются.
- Кривое тело запроса — ошибка ввода, а не авария: `_json_body()` возвращает
  `None`, эндпоинт отвечает внятной ошибкой. Раньше `await request.json()`
  выпускал `JSONDecodeError` наружу и панель получала 500 с трейсбеком.
- e2e-фикстуры обязаны снимать stdio-детей после себя: терминация
  приложения не убивает `mcp-proxy` и его ребёнка, они переживают
  родителя, копятся между прогонами и держат файлы сборки открытыми.

## Тестирование

Проверяется не дерево исходников, а поставляемый артефакт: именно frozen
сборка, а не `run.py`, показала дефект с `mcp.server.fastmcp` в
v0.1.1 — при зелёном `pytest tests` из исходников.

| Уровень | Файл | Что доказывает |
|---|---|---|
| контракт каталога | `tests/test_catalog_contract.py` | единый source contract, egress proxy, сохранение cache, bounded fallback, пагинация |
| рантайм-реестр | `tests/test_runtime_registry.py` | атомарные add/remove, выживание watchdog, безопасный ответ гейтвея, конкурентный snapshot |
| restart-политика | `tests/test_restart_policy.py` | конечное число спаунов, backoff, circuit breaker, ручной reset, изоляция соседей |
| порты | `tests/test_gateway_port.py` | свободный/занятой порт, bindability, исчерпание |
| поставка | `tests/test_ship_hygiene.py` | безопасный example config, инсталлятор, CI, версия, маскирование |
| e2e исходников | `tests/test_e2e.py` | реальный запуск, kill/respawn с новым pid, живой MCP-канал |
| **frozen-артефакт** | `tests/test_frozen_e2e.py` | запуск `AOMG.exe` без консоли, авто-выбор порта, чистый автоконфиг с фактически занятым портом, соответствие легенды панели этому порту, сервер добавленный в рантайме (502/504, не 404), respawn убитого ребёнка с новым pid, занятый порт → код выхода 2 |

Frozen-тесты скипаются, если сборки нет, поэтому `pytest tests` на машине
без PyInstaller остаётся зелёным. CI (Windows-only) сначала гоняет тесты,
потом собирает артефакты.

## Компоненты

```
run.py            точка входа: выбор порта, config load, uvicorn-поток,
                  pystray в main; AOMG_WATCH_INTERVAL переопределяет
                  период watchdog (для e2e)
aomg/config.py    загрузка/сохранение config.yaml, atomic write;
                  ${VAR} подставляется из process-env и HKCU\Environment
aomg/supervisor.py  ВЛАДЕЕЦ рантайм-состояния: add/remove/restart,
                  спавн stdio-детей (порт-прокси pair, "--" перед командой),
                  backoff + circuit breaker, маскирование секретов в логах
aomg/port.py      выбор порта гейтвея с проверкой занятости
aomg/gateway.py   FastAPI: catch-all /{name}/mcp -> прокси к ребёнку
                  или апстриму; /health; /admin/api/logs/{name}
aomg/watchdog.py  проба initialize+tools/list через egress группы;
                  политики нет — она в Supervisor
aomg/registry.py  клиент registry.modelcontextprotocol.io (/v0/servers),
                  sync + async версии
aomg/index.py     локальный индекс каталога: полный слив реестра на диск
                  (cursor-пагинация, дедуп по короткому имени), мгновенный
                  поиск по индексу, stale-while-revalidate в фоне;
                  упавший реестр не смывает индекс; кэш разбора
aomg/catalog.py   встроенный источник neuraldeep.ru под общим контрактом
aomg/json_source.py  пользовательские JSON-источники, кэш на диске,
                  прокси своей egress-группы
aomg/admin.py     веб-панель: статусы, каталог с пагинацией, источники,
                  форма ключей, рестарт; добавление сервера создаёт
                  Health вместе с ManagedServer
aomg/client_sync.py  прописывание url-записей в конфиг MCP-клиента
aomg/tray.py      pystray: цвет по Health-агрегату, меню, "Перезапустить всё"
```

## Критерии (BDD, проверяемые)

1. stdio-сервер из конфига спавнится; tools/list через гейтвей отдаёт его тулы.
2. Убить ребёнка извне → супервизор перезапускает, pid меняется;
   следующий tools/list OK. (tests/test_e2e.py)
3. HTTP-апстрим через группу с прокси: запрос уходит с proxy-клиентом группы.
4. Апстрим недоступен, egress-канал недоступен → state=channel_down (не down).
5. Апстрим отвечает 500/битым JSON-RPC → state=down.
6. client_sync: после запуска в конфиге клиента есть url-запись; idempotent.
7. Registry: search отдаёт записи; для записей с headers → форма содержит
   поле секрета; для packages → kind=stdio + pin версии.
7a. Каталог: поиск по локальному индексу отвечает < 1 c при любом качестве
   сети; индекс старше 24 ч обновляется в фоне, не блокируя ответ; падение
   реестра не удаляет индекс.
7b. Рестарт-цикл конечен: после MAX_FAILS попыток состояние `down` с
   причиной из лога, рестарты прекращаются. (tests/test_restart_policy.py)
7c. Сервер, добавленный в панели после старта, доступен на /<name>/mcp
   без перезапуска приложения. (tests/test_runtime_registry.py)
8. Watchdog пишет историю Health; иконка = worst-of (down красная,
   channel_down/reconnecting жёлтая, ok зелёная).
9. Занятый порт: авто-режим подбирает свободный, явный — диагностика и
   код выхода 2. (tests/test_gateway_port.py)
10. Секрет из конфига не появляется в /health и /admin/api/logs.
    (tests/test_ship_hygiene.py)

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
