# План remediations: аудит сессии 20260930_100646_31e2ee

Дата: 2026-09-30. Правило: **RED доказан до фикса**, фазы идут по порядку,
каждая — отдельный коммит.

## Фаза 0 — сломанное в рабочем дереве

Сейчас `main` содержит незакоммиченную правку `admin.py:230`, которая
ломает каталог целиком (`NameError: name 'request' is not defined`,
`1 failed, 44 passed`). Это не «незавершённая работа» — это регресс.

- RED: `tests/test_admin_sources_api.py::test_search_default_is_official`
- FIX: `request: Request` в сигнатуре `api_catalog`.
- Плюс `?n=` → параметр `limit` (см. ADR-0005, п. 5.4).

## Фаза 1 — Runtime-реестр (ADR-0003)

| # | Тест (RED) | Фикс |
|---|---|---|
| 1.1 | `test_add_creates_health_and_managed` | `Supervisor.add()` — атомарно Health + ManagedServer |
| 1.2 | `test_watchdog_survives_runtime_added_server` | `watchdog` берёт `supervisor.health(name)`, None → skip |
| 1.3 | `test_gateway_connect_error_on_new_server_returns_504` | `gateway` не падает KeyError → 504 |
| 1.4 | `test_remove_is_atomic_all_three_dicts` | `Supervisor.remove()` снимает из обоих |
| 1.5 | `test_snapshot_is_consistent_under_concurrent_add` | `snapshot()` под локом |
| 1.6 | `test_no_direct_healths_indexing_outside_supervisor` | grep-инвариант I2 |

## Фаза 2 — Политика перезапуска (ADR-0004)

| # | Тест (RED) | Фикс |
|---|---|---|
| 2.1 | `test_restart_count_bounded_by_max_fails` (I1) | backoff + circuit breaker в `Supervisor` |
| 2.2 | `test_backoff_delays_second_restart` | `RESTART_BACKOFF` |
| 2.3 | `test_success_resets_failure_chain` (I2) | сброс на `probe()==ok` |
| 2.4 | `test_manual_restart_resets_and_closes_circuit` (I3) | `api_restart` → `reset_failures` |
| 2.5 | `test_down_error_contains_log_tail_reason` | в `Health.error` — причина из лога |
| 2.6 | `test_http_server_never_auto_restarted` (I4) | политика только для stdio |
| 2.7 | `test_dead_command_detected_before_spawn` | предварительная проверка существования command |

## Фаза 3 — Контракт каталога (ADR-0005)

| # | Тест (RED) | Фикс |
|---|---|---|
| 3.1 | `test_every_source_has_search_and_status` (I1) | `NeuralDeepSource.search/status`; `OfficialSource` удалён |
| 3.2 | `test_neuraldeep_source_search_returns_cards` | адаптер под новый контракт |
| 3.3 | `test_json_source_uses_group_proxy` (I2) | `proxy=` из `spec.group` |
| 3.4 | `test_upsert_keeps_other_sources_alive` (I3) | инкрементальная регистрация |
| 3.5 | `test_live_registry_fallback_is_bounded` (I4) | async httpx, timeout 3 с, `notice` |
| 3.6 | `test_catalog_response_has_total_and_has_more` (I5) | пагинация |
| 3.7 | `test_index_search_uses_memory_cache` | кэш по `mtime` (33 мс → ~0) |
| 3.8 | `test_status_reports_last_attempt_not_lazy_ok` | статус = результат попытки |

## Фаза 4 — Платформа и порт (ADR-0006)

| # | Тест (RED) | Фикс |
|---|---|---|
| 4.1 | `test_busy_preferred_port_returns_free_one` (I1) | `resolve_gateway_port` |
| 4.2 | `test_explicit_config_port_not_silently_changed` (I2) | диагностика + код выхода 2 |
| 4.3 | `test_all_ports_busy_raises_readable_error` (I3) | своё исключение |
| 4.4 | `test_legend_uses_configured_port` (I4) | убрать хардкод 9300 из HTML |

## Фаза 5 — Поставка и утечки

| # | Тест | Фикс |
|---|---|---|
| 5.1 | `test_example_config_has_no_dead_servers` | `config.example.yaml` без мёртвых `uvx`/`C:/tools` |
| 5.2 | `test_installer_does_not_ship_working_config` | `installer.iss` не кладёт example как рабочий |
| 5.3 | `test_logs_endpoint_redacts_secret_values` | маскирование `log_tail`/логов по значениям из конфига |
| 5.4 | `test_single_restart_endpoint` | удалить дубль `/admin/restart/{name}` |

## Фаза 6 — CI и документация

- `build.yml`: матрица только `windows-latest`,release отдаёт один тарбол.
- `requirements.txt`: `pystray; sys_platform == "win32"`.
- `SDD.md`: новые BDD-критерии (I1–I4 каждого ADR), раздел «Windows-only».
- `README.md`: quick start, «почему нет Linux/macOS», фактический порт.
- `CHANGELOG.md`: 0.1.3.

## Отложено сознательно (не в этом заходе)

- Разделение `admin.py` (658 строк, HTML+JS строкой) на статику —
  отдельная задача, требует сборки фронта; в этом заходе только
  фиксируется TODO со ссылкой на ADR.
- Аутентификация локального API — панель слушает 127.0.0.1; токен/Origin
  — отдельное решение (ADR-0007 кандидат).
- Реальный `n`-уровень пагинации каталога на стороне реестра (курсор
  `/v0/servers`) — сейчас offset по локальному индексу.

## Гейт перед релизом

1. `python -m pytest tests -q` — 0 failed, ни одного skipped без причины.
2. Ручная проверка: `run.py --no-tray` на чистом конфиге, добавление
   MCP через панель, watchdog жив, каталог отдаёт карточки по всем
   источникам, порт подхватывается при занятом 9300.
3. Сборка `pyinstaller AOMG.spec` + `mcpproxy.spec`, инсталлятор, установка
   в чистый каталог, запуск, скриншот панели.
4. Только после «ок» пользователя — коммит, тег `v0.1.3`, релиз.
