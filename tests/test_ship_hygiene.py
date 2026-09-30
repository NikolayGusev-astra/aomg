"""Поставка и утечки: конфиг-пример, инсталлятор, маскирование логов (ADR-0006, аудит P2)."""
import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

ROOT = pathlib.Path(__file__).resolve().parents[1]


# ---------- config.example.yaml: без мёртвых серверов ----------

def test_example_config_has_no_dead_servers():
    """Демо-сервер, который падает на любой машине, не должен уезжать
    в поставку: он превращается в бесконечный рестарт-цикл (аудит P0)."""
    import yaml
    raw = yaml.safe_load((ROOT / "config.example.yaml").read_text("utf-8"))
    servers = raw.get("servers") or {}
    for name, spec in servers.items():
        cmd = (spec or {}).get("command")
        if not cmd:
            continue
        assert "C:/tools" not in cmd and "C:\\tools" not in cmd, \
            f"{name}: путь из документации не существует на машине юзера"
        if "uvx" in cmd:
            pytest.fail(
                f"{name}: uvx-пакет без пина тянет mcp 2.x и падает на "
                f"initialize — в example он не нужен")


def test_example_config_ships_no_dead_command_at_all():
    """Серверов-примеров с живыми командами в example быть не должно:
    example — это схема, а не рабочая поставка."""
    import yaml
    raw = yaml.safe_load((ROOT / "config.example.yaml").read_text("utf-8"))
    assert not (raw.get("servers") or {}), \
        "config.example.yaml должен быть схемой без серверов"


def test_example_config_documents_both_kinds():
    raw = (ROOT / "config.example.yaml").read_text("utf-8")
    assert "command:" in raw and "url:" in raw, \
        "пример должен показывать оба вида серверов (в комментариях)"


# ---------- installer.iss ----------

def test_installer_does_not_ship_working_config():
    """example не должен попадать в {app} как будто это рабочий конфиг."""
    iss = (ROOT / "installer.iss").read_text("utf-8")
    for line in iss.splitlines():
        if "config.example.yaml" in line and "Source:" in line:
            assert "config.yaml" not in line, \
                "config.example.yaml нельзя класть как config.yaml"
    assert 'Source: "config.example.yaml"; DestDir: "{app}"' not in iss, \
        "example-файл не должен жить рядом с exe безымянным"


def test_installer_version_matches_project():
    iss = (ROOT / "installer.iss").read_text("utf-8")
    m = re.search(r'#define MyAppVersion "([^"]+)"', iss)
    assert m, "в installer.iss обязана быть версия"
    assert m.group(1) == read_version(), \
        f"версия в инсталляторе {m.group(1)} != в проекте {read_version()}"


def read_version() -> str:
    import tomllib  # noqa: F401  (3.11+)
    for name in ("pyproject.toml",):
        p = ROOT / name
        if p.exists():
            return re.search(r'version\s*=\s*"([^"]+)"',
                             p.read_text("utf-8")).group(1)
    return re.search(r'__version__\s*=\s*"([^"]+)"',
                     (ROOT / "aomg" / "__init__.py").read_text("utf-8")).group(1)


def test_project_version_is_declared():
    assert re.match(r"^\d+\.\d+\.\d+$", read_version())


# ---------- маскирование секретов в логах ----------

def test_logs_endpoint_redacts_secret_values():
    """/health и /admin/api/logs не должны выдавать значения секретов."""
    from aomg.supervisor import redact_secrets
    text = ("Authorization: Bearer sk-live-AAAABBBBCCCC\n"
            "JIRA_PAT=pat-value-1234\n"
            "обычная строка лога\n")
    out = redact_secrets(text, ["sk-live-AAAABBBBCCCC", "pat-value-1234"])
    assert "sk-live-AAAABBBBCCCC" not in out
    assert "pat-value-1234" not in out
    assert "обычная строка лога" in out, "не-секреты остаются читаемыми"


def test_redact_ignores_empty_and_short_secrets():
    from aomg.supervisor import redact_secrets
    out = redact_secrets("secret=ab\nother=xyz", ["", None, "a"])
    assert out == "secret=ab\nother=xyz"


def test_redact_replaces_inside_url():
    from aomg.supervisor import redact_secrets
    out = redact_secrets("https://u:p4ssw0rd@host/mcp", ["p4ssw0rd"])
    assert "p4ssw0rd" not in out and "host/mcp" in out


def test_health_log_tail_is_redacted(tmp_path, monkeypatch):
    """Сквозная проверка: секрет из конфига не всплывает в /health."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from aomg import supervisor as sv
    from aomg.config import Config, Group, ServerSpec
    from aomg.gateway import create_app
    from aomg.supervisor import Supervisor

    logs = tmp_path / "logs"
    sv.set_logs_dir(logs)
    (logs / "srv.log").write_text(
        "подключаюсь с ключом sk-live-SECRETVALUE\n", encoding="utf-8")

    cfg = Config(gateway_port=9389, groups={"direct": Group(name="Дом")})
    spec = ServerSpec(name="srv", kind="http", url="http://x.invalid/mcp",
                      headers={"Authorization": "Bearer sk-live-SECRETVALUE"})
    cfg.servers["srv"] = spec
    sup = Supervisor(cfg)
    sup.add("srv", spec)
    sup.set_secrets(["sk-live-SECRETVALUE"])

    c = TestClient(create_app(cfg, sup))
    body = c.get("/health").text
    assert "sk-live-SECRETVALUE" not in body, "секрет утёк в /health"
    assert "подключаюсь" in body, "остальной лог остаётся полезным"
    sv.set_logs_dir(None)


def test_secrets_collected_from_config_env_and_headers():
    """Секреты собираются из env и headers серверов автоматически."""
    from aomg.config import Config, Group, ServerSpec
    from aomg.supervisor import Supervisor
    cfg = Config(gateway_port=9388, groups={"direct": Group(name="Дом")})
    cfg.servers["a"] = ServerSpec(name="a", kind="http", url="http://x/mcp",
                                  headers={"Authorization": "Bearer TOPSECRET"},
                                  env={"API_KEY": "ENV-SECRET-9"})
    sup = Supervisor(cfg)
    sup.collect_secrets()
    joined = " ".join(sup.secrets)
    assert "TOPSECRET" in joined and "ENV-SECRET-9" in joined


def test_placeholder_expansion_is_not_treated_as_secret():
    """Неразвёрнутый ${VAR} не должен попадать в список секретов
    (иначе redactor затрёт текст вида ${GITHUB_PAT})."""
    from aomg.supervisor import collect_secret_values
    vals = collect_secret_values(env={"A": "${B}"}, headers={"H": "${C}"})
    assert "${B}" not in vals and "${C}" not in vals


# ---------- один endpoint рестарта ----------

def test_single_restart_endpoint():
    """Дубль /admin/restart/{name} и /admin/api/servers/{n}/restart — два
    контракта на одно действие (аудит P1 #8)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from aomg.admin import register_admin
    from aomg.gateway import create_app
    from aomg.config import Config, Group
    from aomg.supervisor import Supervisor
    import tempfile
    from pathlib import Path

    cfg = Config(gateway_port=9387, groups={"direct": Group(name="Дом")})
    sup = Supervisor(cfg)
    app = create_app(cfg, sup)
    with tempfile.TemporaryDirectory() as td:
        cfg_file = Path(td) / "config.yaml"
        cfg_file.write_text(
            "gateway_port: 9387\n"
            "groups:\n  direct: {name: 'Дом', proxy: null}\n"
            "servers: {}\n", encoding="utf-8")
        register_admin(app, cfg, sup, cfg_file, restart_watchdog=lambda: None)

    paths = {r.path for r in app.routes}
    assert "/admin/restart/{name}" not in paths, \
        "устаревший дубль рестарта должен быть удалён (ADR-0003)"
    assert "/admin/api/servers/{name}/restart" in paths, \
        "у рестарта должен быть один канонический endpoint"
