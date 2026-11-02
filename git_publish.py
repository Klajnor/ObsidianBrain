"""Публикация только созданной заметки, без чужих staged-файлов и коммитов."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

_SUBJECT = "Telegram: сохранена заметка"


def _failure_reason(stderr: str) -> str:
    """Классифицировать stderr, не раскрывая URL, ключи и имена файлов."""
    text = stderr.casefold()
    if "dubious ownership" in text:
        return "небезопасный владелец Git (dubious ownership)"
    if any(part in text for part in ("author identity unknown", "unable to auto-detect email", "empty ident name")):
        return "не настроены user.name/user.email"
    if "could not resolve host" in text or "could not resolve hostname" in text:
        return "сбой DNS Git"
    if any(part in text for part in ("authentication failed", "permission denied (publickey)", "could not read username")):
        return "аутентификация Git не удалась"
    if "read-only file system" in text:
        return "read-only file system"
    if "permission denied" in text:
        return "permission denied"
    if "not a git repository" in text:
        return "not a git repository"
    if "non-fast-forward" in text or "fetch first" in text:
        return "remote изменился; автоматический rebase/force запрещён"
    return "причина скрыта: stderr может содержать секреты"


def publish_note(vault: Path, target: Path) -> None:
    """Коммит и обычный push в upstream; при сбое локальная заметка остаётся."""
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    # Не наследуем управление индексом/репозиторием от вызывающего процесса.
    for name in ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE"):
        environment.pop(name, None)

    def git(*args: str, env: dict[str, str] | None = None) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(vault), *args],
                env=env or environment, capture_output=True, timeout=60,
                encoding="utf-8", errors="replace", check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("Git недоступен или превышен таймаут; заметка сохранена локально") from exc
        if result.returncode:
            # stderr может содержать URL с токеном, имена и содержимое файлов.
            raise RuntimeError(f"Ошибка Git ({args[0]}): {_failure_reason(result.stderr)}; заметка сохранена локально")
        return result.stdout.strip()

    root = Path(git("rev-parse", "--show-toplevel")).resolve()
    if root != vault.resolve():
        raise RuntimeError("Хранилище должно быть корнем Git-репозитория")
    relative = target.relative_to(vault).as_posix()
    if git("diff", "--cached", "--name-only", "--", relative):
        raise RuntimeError("Созданный файл уже staged: чужой индекс не изменён")
    branch = git("symbolic-ref", "HEAD")
    short_branch = branch.removeprefix("refs/heads/")
    remote = git("config", "--get", f"branch.{short_branch}.remote")
    merge = git("config", "--get", f"branch.{short_branch}.merge")
    if not remote or remote == "." or not merge.startswith("refs/heads/"):
        raise RuntimeError("Для ветки хранилища нужен upstream удалённого репозитория")
    git("fetch", "--", remote)
    head = git("rev-parse", "HEAD")
    upstream = git("rev-parse", "@{upstream}")
    # Не выполняем merge/rebase и не меняем чужие рабочие файлы.
    git("merge-base", "--is-ancestor", upstream, head)
    outgoing = git("rev-list", f"{upstream}..{head}").splitlines()
    for commit in outgoing:
        changed = git("diff-tree", "--no-commit-id", "--name-only", "-r", commit).splitlines()
        subject = git("show", "-s", "--format=%s", commit)
        if changed != [relative] or subject != _SUBJECT:
            raise RuntimeError("Есть посторонние локальные коммиты: автоматический push отменён")

    # Отдельный индекс гарантирует, что чужие staged-файлы не попадут в коммит.
    published_head = head
    with tempfile.TemporaryDirectory(prefix="brain-git-") as temp:
        isolated = dict(environment, GIT_INDEX_FILE=str(Path(temp) / "index"))
        git("read-tree", head, env=isolated)
        git("add", "--", relative, env=isolated)
        tree = git("write-tree", env=isolated)
        current_tree = git("rev-parse", f"{head}^{{tree}}")
        if tree != current_tree:
            commit = git("commit-tree", tree, "-p", head, "-m", _SUBJECT, env=isolated)
            # CAS: чужой параллельный коммит не перезаписывается.
            git("update-ref", branch, commit, head)
            published_head = commit
            # Синхронизируем только собственный путь в основном индексе.
            git("reset", "-q", commit, "--", relative)
    # Без force: удалённые изменения не перезаписываются.
    git("push", "--no-follow-tags", "--recurse-submodules=no", "--", remote,
        f"{published_head}:{merge}")
