"""Проверка готовности записи Telegram-заметок и публикации в Git.

Запускать тем же пользователем и с теми же переменными, что у MCP-сервера:
    BRAIN_WRITE_ENABLED=true .venv/bin/python write_doctor.py
Для проверки доступа к remote добавьте --network (может ждать до 30 секунд).
Секреты, remote URL и содержимое заметок не печатаются.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from common import DB_PATH, VAULTS

OK = "✅"
WARN = "⚠️"
FAIL = "❌"


def _reason(stderr: str) -> str:
    """Безопасная категория ошибки Git без полного stderr."""
    try:
        from git_publish import _failure_reason

        return _failure_reason(stderr)
    except Exception:
        return "причина скрыта"


def _git(vault: Path, *args: str, timeout: float = 20.0) -> tuple[str | None, str | None]:
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run(
            ["git", "-C", str(vault), *args],
            env=environment,
            capture_output=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"Git недоступен или превышен таймаут ({type(exc).__name__})"
    if result.returncode:
        return None, f"Git ({args[0]}): {_reason(result.stderr)}"
    return result.stdout.strip(), None


def _write_probe(directory: Path, label: str) -> tuple[bool, str]:
    """Создать и удалить временный файл; не оставлять мусор в репозитории."""
    try:
        with tempfile.NamedTemporaryFile(
            dir=directory, prefix=".brain-write-doctor-", delete=True
        ) as stream:
            stream.write(b"probe")
            stream.flush()
        return True, f"{label}: запись разрешена"
    except OSError as exc:
        return False, f"{label}: нет записи ({type(exc).__name__})"


def _check_database() -> tuple[bool, str]:
    parent = DB_PATH.parent
    if not parent.exists():
        try:
            parent.mkdir(parents=True)
        except OSError as exc:
            return False, f"SQLite: не создать {parent} ({type(exc).__name__})"
    if not DB_PATH.exists():
        writable, message = _write_probe(parent, "SQLite-каталог")
        return writable, message + "; файл БД будет создан"
    try:
        db = sqlite3.connect(f"file:{DB_PATH}?mode=rw", uri=True, timeout=5)
        try:
            db.execute("BEGIN IMMEDIATE")
            db.rollback()
        finally:
            db.close()
        return True, "SQLite: запись разрешена"
    except (OSError, sqlite3.Error) as exc:
        return False, f"SQLite: нет записи ({type(exc).__name__})"


def check(network: bool) -> list[tuple[bool, str]]:
    results: list[tuple[bool, str]] = []
    enabled = os.environ.get("BRAIN_WRITE_ENABLED", "false").lower() == "true"
    results.append(
        (
            enabled,
            "BRAIN_WRITE_ENABLED=true"
            if enabled
            else "BRAIN_WRITE_ENABLED не true: create_note не зарегистрирован. "
            "Передайте переменную в env сервера brain в .mcp.json",
        )
    )
    if not VAULTS:
        return results + [(False, "VAULT_PATH не задан")]
    vault = VAULTS[0].resolve()
    results.append((vault.is_dir(), f"Хранилище: {vault}"))
    if not vault.is_dir():
        return results
    writable, message = _write_probe(vault, "Корень хранилища")
    results.append((writable, message))

    telegram = vault / "Telegram"
    if telegram.is_symlink() or (telegram.exists() and not telegram.is_dir()):
        results.append((False, "Telegram: путь занят не каталогом или является симлинком"))
    elif telegram.exists():
        writable, message = _write_probe(telegram, "Telegram/")
        results.append((writable, message))
    else:
        results.append((True, "Telegram/: ещё нет, будет создан при записи"))

    results.append(_check_database())

    root, error = _git(vault, "rev-parse", "--show-toplevel")
    if error or root is None:
        results.append((False, f"Git: {error or 'пустой ответ'}"))
        return results
    results.append((Path(root).resolve() == vault, "Git: хранилище является корнем репозитория"))
    if Path(root).resolve() != vault:
        return results

    branch, error = _git(vault, "symbolic-ref", "--short", "HEAD")
    results.append((branch is not None, f"Git: ветка {branch}" if branch else f"Git: {error}"))
    if not branch:
        return results

    remote, error = _git(vault, "config", "--get", f"branch.{branch}.remote")
    merge, error_merge = _git(vault, "config", "--get", f"branch.{branch}.merge")
    upstream_ok = bool(remote and merge and merge.startswith("refs/heads/"))
    results.append(
        (
            upstream_ok,
            f"Git: upstream {remote}/{merge.removeprefix('refs/heads/')}"
            if upstream_ok
            else f"Git: не настроен upstream ({error or error_merge or 'нет branch.*.remote/merge'})",
        )
    )

    for key in ("user.name", "user.email"):
        value, error = _git(vault, "config", "--get", key)
        results.append((bool(value), f"Git: {key} {'задан' if value else 'не задан'}"))

    writable, message = _write_probe(vault / ".git", "Git: каталог .git")
    results.append((writable, message))

    if upstream_ok:
        staged, error = _git(vault, "diff", "--cached", "--name-only")
        if error:
            results.append((False, f"Git: {error}"))
        else:
            results.append(
                (
                    not staged,
                    "Git: индекс чист"
                    if not staged
                    else "Git: есть staged-файлы; публикация не изменяет их, но проверьте вручную",
                )
            )
        outgoing, error = _git(vault, "rev-list", "--count", "@{upstream}..HEAD")
        if error:
            results.append((False, f"Git: {error}"))
        elif outgoing and int(outgoing) > 0:
            results.append(
                (
                    False,
                    f"Git: {outgoing} локальных коммитов впереди upstream; "
                    "автоматический push заблокируется, если среди них есть чужие",
                )
            )
        else:
            results.append((True, "Git: локальных коммитов впереди upstream нет"))

        if network:
            _, error = _git(vault, "ls-remote", "--exit-code", "--heads", remote, timeout=30)
            results.append((not error, f"Git: доступ к remote {'есть' if not error else error}"))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверка записи во второй мозг")
    parser.add_argument("--network", action="store_true", help="проверить доступ к remote")
    args = parser.parse_args()
    results = check(args.network)
    for ok, message in results:
        print(f"{OK if ok else FAIL} {message}")
    if any("user.name не задан" in message or "user.email не задан" in message for _, message in results):
        vault = VAULTS[0] if VAULTS else Path("VAULT_PATH")
        print()
        print("Задайте identity локально в репозитории (не только в ~/.gitconfig):")
        print("MCP-процесс может не видеть домашний каталог из-за ProtectHome):")
        print(f"sudo -u brain git -C '{vault}' config user.name 'Kapum ChatBot'")
        print(f"sudo -u brain git -C '{vault}' config user.email 'bot@example.com'")
        print("Укажите свой GitHub-email или noreply-адрес, затем повторите doctor.")
    failed = sum(not ok for ok, _ in results)
    print()
    print("Итог: всё готово" if not failed else f"Итог: проблем — {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
