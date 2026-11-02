"""Синхронизация хранилища с origin и публикация только созданной заметки."""

from __future__ import annotations

import os
import shutil
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
    if "host key verification failed" in text:
        return "SSH host key не подтверждён"
    if any(part in text for part in ("no such identity", "identity file", "load key")):
        return "SSH-ключ недоступен"
    if any(part in text for part in (
        "authentication failed", "permission denied (publickey)", "could not read username",
        "could not read from remote repository", "repository not found", "terminal prompts disabled",
    )):
        return "аутентификация Git не удалась"
    if "connection timed out" in text or "connection refused" in text:
        return "сетевое подключение к Git не удалось"
    if "cannot lock ref" in text or "index.lock" in text:
        return "Git занят другим процессом"
    if "read-only file system" in text:
        return "read-only file system"
    if "permission denied" in text:
        return "permission denied"
    if "not a git repository" in text:
        return "not a git repository"
    if "non-fast-forward" in text or "fetch first" in text:
        return "remote изменился; автоматический rebase/force запрещён"
    return "причина скрыта: stderr может содержать секреты"


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    # Не наследуем управление индексом/репозиторием от вызывающего процесса.
    for name in ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE"):
        environment.pop(name, None)
    return environment


def _run(
    vault: Path,
    *args: str,
    env: dict[str, str] | None = None,
    timeout: float = 60.0,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(vault), *args],
            env=env or _environment(),
            capture_output=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Git недоступен или превышен таймаут") from exc


def _git(
    vault: Path,
    *args: str,
    env: dict[str, str] | None = None,
    timeout: float = 60.0,
    note: bool = True,
) -> str:
    result = _run(vault, *args, env=env, timeout=timeout)
    if result.returncode:
        suffix = "заметка сохранена локально" if note else "заметка не создана"
        raise RuntimeError(
            f"Ошибка Git ({args[0]}): {_failure_reason(result.stderr)}; {suffix}"
        )
    return result.stdout.strip()


def _remove_conflicting_untracked(vault: Path, upstream: str) -> None:
    """Удалить только untracked-файлы, которые origin уже отслеживает."""
    tracked = set(_git(vault, "ls-tree", "-r", "--name-only", upstream, note=False).splitlines())
    status = _run(vault, "status", "--porcelain", "-z", "--untracked-files=all")
    if status.returncode:
        raise RuntimeError("Не удалось прочитать состояние хранилища перед reset")
    for entry in status.stdout.split("\0"):
        if not entry.startswith("?? "):
            continue
        relative = entry[3:]
        if relative not in tracked:
            continue
        victim = vault / relative
        try:
            victim.resolve().relative_to(vault.resolve())
        except (OSError, ValueError):
            continue
        if victim.is_symlink() or victim.is_file():
            victim.unlink()
        elif victim.is_dir():
            shutil.rmtree(victim)
        parent = victim.parent
        while parent != vault:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent


def sync_origin(vault: Path) -> str:
    """Fetch и выровнять рабочее дерево по origin.

    Обычный fast-forward сохраняет локальные файлы. Если pull невозможен из-за
    локальных изменений или расхождения веток, origin считается главным:
    tracked-изменения и локальные коммиты отбрасываются, а untracked-файлы
    удаляются только там, где origin уже содержит такой путь.
    """
    root = Path(_git(vault, "rev-parse", "--show-toplevel", note=False)).resolve()
    if root != vault.resolve():
        raise RuntimeError("Хранилище должно быть корнем Git-репозитория")
    branch = _git(vault, "symbolic-ref", "HEAD", note=False)
    short_branch = branch.removeprefix("refs/heads/")
    remote = _git(vault, "config", "--get", f"branch.{short_branch}.remote", note=False)
    merge = _git(vault, "config", "--get", f"branch.{short_branch}.merge", note=False)
    if not remote or remote == "." or not merge.startswith("refs/heads/"):
        raise RuntimeError("Для ветки хранилища нужен upstream удалённого репозитория")

    fetched = _run(vault, "fetch", "--prune", "--", remote, timeout=90)
    if fetched.returncode:
        raise RuntimeError(
            f"Ошибка Git (fetch): {_failure_reason(fetched.stderr)}; заметка не создана"
        )
    upstream = _git(vault, "rev-parse", "@{upstream}", note=False)
    head = _git(vault, "rev-parse", "HEAD", note=False)
    if head == upstream:
        return "up-to-date"

    merged = _run(vault, "merge", "--ff-only", upstream, timeout=90)
    if merged.returncode == 0:
        return "fast-forward"

    _remove_conflicting_untracked(vault, upstream)
    reset = _run(vault, "reset", "--hard", upstream, timeout=90)
    if reset.returncode:
        raise RuntimeError(
            f"Ошибка Git (reset): {_failure_reason(reset.stderr)}; заметка не создана"
        )
    return "reset-to-origin"


def publish_note(vault: Path, target: Path) -> None:
    """Коммит и обычный push в upstream; при сбое локальная заметка остаётся."""

    def git(*args: str, env: dict[str, str] | None = None) -> str:
        return _git(vault, *args, env=env, note=True)

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
        isolated = dict(_environment(), GIT_INDEX_FILE=str(Path(temp) / "index"))
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
