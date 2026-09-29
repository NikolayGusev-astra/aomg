# AOMG — Another One MCP Gateway

Трей-приложение для Windows: **супервизор + HTTP-гейтвей + GUI** для локальных
(stdio) и серверных (HTTP) MCP-серверов. Один процесс, один порт — все твои
MCP в одном месте.

![banner](assets/banner.png)

![демо](docs/media/aomg-demo.gif)
*18 секунд: панель, каталог из реестра, watchdog. Видео целиком: [docs/media/aomg-demo.mp4](docs/media/aomg-demo.mp4)*

> Python/MVP-фаза. В планах — порт на Rust (Tauri v2 + rmcp + axum).

## Зачем

Локальные MCP-сервера требуют запуска и контроля состояния, могут не пережить
закрытие агента, зависят от внешнего мониторинга; иногда блокируют файлы
агента и создают проблемы при обновлениях. AOMG выносит их в общий сервис:

- **Один порт** — все MCP доступны как `http://127.0.0.1:9300/<имя>/mcp`
  (Streamable HTTP); агент подключается один раз и навсегда: url-запись не
  меняется при обновлениях и переездах серверов
- **Супервизор** — спавнит stdio-MCP как дочерние процессы (через
  mcp-proxy), рестартует умерших, не блокирует файлы агента
- **Watchdog** — полный MCP-handshake + `tools/list` каждые 30 сек: не
  «TCP открылся», а «сервер реально отвечает». Лежащий VPN = жёлтая
  иконка («канал недоступен»), умерший сервер = красная
- **GUI** — веб-панель `http://127.0.0.1:9300/admin`: статусы, каталог с
  поиском по официальному реестру MCP, форма добавления с полями ключей,
  «Перезапустить всё». Без процессов, портов и env в интерфейсе
- **Egress-группы** — per-connection маршрутизация (напрямую / SOCKS5-VPN /
  корпоративный прокси) без маршрутов Windows и админ-прав
- **Секреты** — в конфиге только `${VAR}`-плейсхолдеры (подстановка из
  окружения при старте); GUI никогда не возвращает ключи

## Быстрый старт

```bash
pip install -r requirements.txt
copy config.example.yaml config.yaml   # поправь под себя
python run.py                          # иконка в трее
```

Пример конфига:

```yaml
gateway_port: 9300

groups:
  direct:    {name: "Дом (напрямую)", proxy: null}
  vpn:       {name: "Через VPN",      proxy: "socks5://127.0.0.1:10808"}
  corporate: {name: "Рабочая сеть",   proxy: "http://corp-gw:3128"}

servers:
  weather:     {command: "uvx", args: ["mcp-server-weather"], group: direct}
  github:      {url: "https://api.githubcopilot.com/mcp/", group: vpn,
                headers: {Authorization: "Bearer ${GITHUB_PAT}"}}
  my-corp-mcp: {command: "C:/tools/corp-mcp.exe", group: corporate,
                env: {API_KEY: "${CORP_KEY}", NO_PROXY: "*"}}
```

## Подключение агента

Агент подключается на `http://127.0.0.1:9300/<имя>/mcp` — в любом MCP-клиенте
с поддержкой Streamable HTTP:

```yaml
mcp_servers:
  github:
    url: http://127.0.0.1:9300/github/mcp
```

Запись в конфиге агента не меняется никогда: сервер переезжает с npx на exe,
меняет хост или ключ — правки остаются внутри `config.yaml` AOMG.

Можно автоматизировать: `aomg/client_sync.py` прописывает url-записи в конфиг
клиента сам (идемпотентно, помечает свои записи маркером `x-aomg` и чужие не
трогает).

## Как это работает

```
агент ──HTTP──> gateway :9300/<name>/mcp
                   │
                   ├─ stdio:  spawn mcp-proxy.exe ──stdio──> сервер-MCP.exe
                   └─ http:   проксирование на upstream URL
                   │
                watchdog (30s): initialize + tools/list через тот же egress
```

- HTTP-серверы: апстрим-клиент с per-group прокси (`httpx.AsyncClient`,
  асинхронный — иначе один держащий соединение MCP-клиент вешает gateway)
- stdio-дети: `HTTPS_PROXY/ALL_PROXY` в env при спавне; `NO_PROXY=127.0.0.1`
  зашит намертво — loopback не ходит через прокси
- Секреты в GUI: при чтении конфига ключи маскируются `***` и никогда не
  возвращаются через API

Подробнее: [docs/adr/ADR-0001-architecture.md](docs/adr/ADR-0001-architecture.md),
[docs/SDD.md](docs/SDD.md).

## Каталог

Веб-панель ищет серверы в официальном реестре
[registry.modelcontextprotocol.io](https://registry.modelcontextprotocol.io)
(`/v0/servers?search=...`): title, описание, remotes/packages, поля ключей
из `headers` c `isSecret` — форма подстраивается под сервер, не хардкод.
Плюс адаптер для сторонних каталогов (в комплекте — пример с RSC-парсингом).

## Трей

Строка статуса, «Открыть управление» (двойной клик открывает веб-панель),
«Перезапустить всё», «Выход». Цвет точки на иконке = агрегатное состояние:
зелёный — всё работает, жёлтый — рестарт/нет сети, красный — сервер умер.

## Сборка exe

```bash
pip install pyinstaller
python -m PyInstaller AOMG.spec --noconfirm
python -m PyInstaller mcpproxy.spec --noconfirm
# dist/AOMG.exe + dist/mcp-proxy.exe (кладём рядом)
```

## Известные грабли (если собираешь сами)

- **mcp-proxy==0.9.0** — пин обязателен: 0.10+ несовместимы с текущим
  `mcp` SDK; endpoint прокси требует trailing slash `/mcp/`
- **frozen-сборка**: `mcp_proxy/__main__.py` как top-level script падает
  с `attempted relative import` — нужна wrapper-точка входа
  (`mpx-entry.py` + `runpy.run_module`); frozen-режим требует пару
  `AOMG.exe` + `mcp-proxy.exe` рядом
- **sync httpx.Client внутри async-обработчика** блокирует event loop:
  один MCP-клиент, держащий SSE-соединение, вешает весь gateway. Только
  `httpx.AsyncClient` + `await`; быстрые curl-пробы этот баг не ловят
- **Path-параметры FastAPI**: литеральный путь + `name` в сигнатуре
  handler'а = `name` уезжает в query (422). Использовать
  `request.path_params["name"]` и маршрут `"/{name}/mcp"`

## Тесты

```bash
python -m pytest tests/ -v
```

Unit + e2e на фейковом stdio-сервере (агрегация, health, рестарт ребёнка
после kill, 404). Один тест каталога требует доступ к официальному реестру —
при недоступности сети он падает, остальное локально.

## Стек и планы

Python 3.11+: pystray + FastAPI/uvicorn + mcp SDK + mcp-proxy +
PyInstaller. Планы: порт на Rust (Tauri v2 + rmcp + axum).

## License

MIT
