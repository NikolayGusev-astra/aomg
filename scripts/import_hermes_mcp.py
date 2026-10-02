"""Импорт mcp_servers из Hermes-конфига в AOMG.

Читает %LOCALAPPDATA%/hermes/config.yaml, переносит серверы в
%AOMG%/config.yaml. Секреты, записанные литералом, заменяются на
${VAR} — только если значение буквально совпадает с переменной
окружения (с учётом префикса `Bearer `). Незакрытые литералы
переносятся как есть и попадают в отчёт: молча уронить токен в
конфиг другого приложения нельзя.

Выключенные (`enabled: false`) серверы не переносятся и перечисляются
отдельно.

Запуск: python import_hermes_mcp.py [--dry-run] [--include-disabled]
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import subprocess
import sys

import yaml

LOCAL = pathlib.Path(os.environ["LOCALAPPDATA"])
HERMES = LOCAL / "hermes" / "config.yaml"
AOMG_DIR = LOCAL / "Programs" / "AOMG"

SECRET_RE = re.compile(r"token|key|secret|pass|auth|pat|credential", re.I)
# `PYTHONPATH` и `*_CREDS_PATH` ловятся по «pass»/«cred», но это пути,
# а не секреты. Подставлять ${PYTHONPATH} особенно нельзя: переменная
# в окружении может указывать на другой venv, чем тот, что в конфиге.
NOT_SECRET_RE = re.compile(r"PATH$|^PYTHONPATH$", re.I)


def version_meets(have: str, need: str) -> bool:
    """Версия `have` не ниже `need`. Сравнение по числовым сегментам.

    Проверено на реальном конфликте: astra-jira-dc-mcp требует
    mcp>=1.20.0 (Icon в mcp.types, meta= в FastMCP.tool()), а auto-rag
    требует >=1.14.0 (починен issubclass на строковых аннотациях). В
    одном venv они не уживутся - поэтому это проверяется на импорте.
    """

    def seg(v: str) -> tuple:
        return tuple(int(x) for x in re.findall(r"\d+", v) or [0])

    a, b = seg(have), seg(need)
    return (a + (0, 0, 0))[:3] >= (b + (0, 0, 0))[:3]


def check_mcp_requirement(python: str, needed: str | None,
                          requirement: dict | None = None) -> tuple | None:
    """Проверить, подходит ли интерпретатор серверу.

    Возвращает (kind, текст) при проблеме либо None. kind='conflict'
    означает реальное расхождение версий - это надо показать сразу,
    а не через минуту после старта, когда сервер упадёт в логе.
    """
    if not needed and not requirement:
        return None
    if needed is None and requirement:
        needed = (requirement or {}).get("mcp")
    if not needed:
        return None
    try:
        r = subprocess.run(
            [python, "-c",
             "import importlib.metadata as m;"
             "print(m.version('mcp'))"],
            capture_output=True, text=True, errors="replace", timeout=60)
        have = (r.stdout or "").strip()
    except (OSError, subprocess.SubprocessError) as e:
        return "unknown", f"не удалось спросить mcp у {python}: {e}"
    if not have:
        return "unknown", f"в {python} пакет mcp не установлен"
    if not version_meets(have, needed):
        return ("conflict",
                f"нужен mcp >= {needed}, в интерпретаторе {have} - "
                f"сервер упадёт при запуске. Дайте ему свой venv.")
    return None


def env_replacements() -> dict:
    """Имя переменной -> значение, только из process-env.

    AOMG подставляет ${VAR} из окружения процесса и HKCU\Environment.
    Сравнивать надо с тем же источником, из которого гейтвей берёт
    значение, иначе ссылка не разрешится.
    """
    return {k: v for k, v in os.environ.items() if v}


def mask_secrets(spec: dict, env: dict) -> tuple[dict, list[str]]:
    """Заменить литеральные секреты на ${VAR}. Возвращает (spec, отчёт)."""
    report: list[str] = []
    for src in ("env", "headers"):
        block = spec.get(src)
        if not isinstance(block, dict):
            continue
        for key, val in list(block.items()):
            val = str(val)
            if not SECRET_RE.search(str(key)) or val.startswith("${"):
                continue
            if NOT_SECRET_RE.search(str(key)):
                continue          # путь, а не секрет
            m = re.match(r"^(?:Bearer\s+)?(.+)$", val)
            bare = m.group(1) if m else val
            for var, value in env.items():
                if SECRET_RE.search(var) and value == bare:
                    prefix = val[: len(val) - len(bare)]
                    block[key] = f"{prefix}${{{var}}}"
                    report.append(f"{src}.{key} -> ${{{var}}}")
                    break
            else:
                report.append(f"{src}.{key} -> ОСТАЛСЯ ЛИТЕРАЛ "
                              f"({len(val)} симв.)")
    return spec, report


def convert(name: str, spec: dict) -> tuple[str, dict] | None:
    out: dict = {"group": "direct"}
    if spec.get("url"):
        out["url"] = spec["url"]
        if spec.get("headers"):
            out["headers"] = dict(spec["headers"])
        return name, out
    cmd = spec.get("command")
    if not cmd:
        return None
    out["command"] = cmd
    if spec.get("args"):
        out["args"] = list(spec["args"])
    if spec.get("env"):
        out["env"] = dict(spec["env"])
    return name, out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--include-disabled", action="store_true")
    args = ap.parse_args()

    cfg = yaml.safe_load(HERMES.read_text(encoding="utf-8")) or {}
    servers = cfg.get("mcp_servers") or {}
    env = env_replacements()

    cfg_path = AOMG_DIR / "config.yaml"
    acfg = (yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            if cfg_path.exists() else {})
    acfg.setdefault("gateway_port", 9300)
    acfg.setdefault("groups",
                    {"direct": {"name": "Напрямую", "proxy": None}})
    acfg.setdefault("servers", {})

    added, skipped, secrets, conflicts = [], [], [], []
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            skipped.append((name, "не словарь"))
            continue
        if spec.get("enabled") is False and not args.include_disabled:
            skipped.append((name, "выключен в Hermes (enabled: false)"))
            continue
        spec, rep = mask_secrets(dict(spec), env)
        secrets += [f"{name}: {r}" for r in rep]
        res = convert(name, spec)
        if res is None:
            skipped.append((name, "нет command и url"))
            continue
        key, value = res
        if "url" not in value:
            # версия mcp в целевом интерпретаторе может не подойти:
            # jira требует >=1.20.0, auto-rag >=1.14.0. Предупреждаем
            # на импорте, а не через минуту после старта (ADR-0005).
            need = spec.get("mcp_requires")
            if need:
                res_check = check_mcp_requirement(value["command"], need)
                if res_check:
                    conflicts.append((key, res_check[0], res_check[1]))
        added.append((key, "http" if "url" in value else "stdio"))
        acfg["servers"][key] = value

    for key, kind in added:
        print(f"  + {key:20} {kind}")
    for key, why in skipped:
        print(f"  - {key:20} {why}")
    if secrets:
        print("\nсекреты:")
        for line in secrets:
            print(f"  {line}")
    if conflicts:
        print("\nКОНФЛИКТ ВЕРСИЙ (сервер упадёт при запуске):")
        for key, kind, why in conflicts:
            print(f"  ! {key:20} {kind}: {why}")

    if args.dry_run:
        print("\ndry-run: файл не изменён")
        return 0

    tmp = cfg_path.with_suffix(".yaml.tmp")
    tmp.write_text(
        "# AOMG config — серверы добавляются через панель\n"
        "# http://127.0.0.1:9300/admin\n"
        + yaml.safe_dump(acfg, allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    os.replace(tmp, cfg_path)
    print(f"\nзаписано в {cfg_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
