"""Настоящий Git на временных локальных репозиториях; без сети и секретов."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from git_publish import _failure_reason, publish_note, sync_origin


class GitPublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.vault = self.root / "vault"
        self.env = patch.dict(os.environ, {
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.git(self.root, "init", "--bare", str(self.remote))
        self.git(self.root, "clone", str(self.remote), str(self.vault))
        self.git(self.vault, "checkout", "-b", "main")
        (self.vault / "existing.md").write_text("initial\n", encoding="utf-8")
        self.git(self.vault, "add", "existing.md")
        self.git(self.vault, "commit", "-m", "Initial")
        self.git(self.vault, "push", "-u", "origin", "main")
        (self.vault / "Telegram").mkdir()
        self.target = self.vault / "Telegram" / "note.md"
        self.target.write_text("captured\n", encoding="utf-8")

    def git(self, directory, *args):
        return subprocess.run(["git", "-C", str(directory), *args], check=True,
                              capture_output=True, encoding="utf-8", timeout=30).stdout.strip()

    def test_failure_reasons_do_not_expose_stderr_secrets(self):
        cases = [
            ("Author identity unknown", "user.name/user.email"),
            ("Could not resolve host: github.com", "DNS"),
            ("Permission denied (publickey)", "аутентификация"),
            ("detected dubious ownership", "dubious ownership"),
            ("unable to create file: Permission denied", "permission denied"),
            ("arbitrary unexpected failure", "причина скрыта"),
        ]
        for stderr, expected in cases:
            with self.subTest(expected=expected):
                reason = _failure_reason(stderr + " https://user:secret-token@example.invalid /secret/vault")
                self.assertIn(expected, reason)
                self.assertNotIn("secret-token", reason)
                self.assertNotIn("/secret/vault", reason)

    def _other_clone(self) -> Path:
        other = self.root / "other"
        self.git(self.root, "clone", "-b", "main", str(self.remote), str(other))
        return other

    def test_sync_fast_forwards_clean_clone(self):
        other = self._other_clone()
        (other / "remote.md").write_text("from origin\n", encoding="utf-8")
        self.git(other, "add", "remote.md")
        self.git(other, "commit", "-m", "Remote")
        self.git(other, "push")
        self.assertEqual(sync_origin(self.vault), "fast-forward")
        self.assertEqual((self.vault / "remote.md").read_text(encoding="utf-8"), "from origin\n")

    def test_sync_discards_conflicting_local_modification(self):
        (self.vault / "existing.md").write_text("local edit\n", encoding="utf-8")
        other = self._other_clone()
        (other / "existing.md").write_text("origin version\n", encoding="utf-8")
        self.git(other, "add", "existing.md")
        self.git(other, "commit", "-m", "Remote edit")
        self.git(other, "push")
        self.assertEqual(sync_origin(self.vault), "reset-to-origin")
        self.assertEqual((self.vault / "existing.md").read_text(encoding="utf-8"), "origin version\n")

    def test_sync_discards_diverged_local_commit(self):
        (self.vault / "local.md").write_text("local commit\n", encoding="utf-8")
        self.git(self.vault, "add", "local.md")
        self.git(self.vault, "commit", "-m", "Local")
        other = self._other_clone()
        (other / "remote.md").write_text("remote\n", encoding="utf-8")
        self.git(other, "add", "remote.md")
        self.git(other, "commit", "-m", "Remote")
        self.git(other, "push")
        self.assertEqual(sync_origin(self.vault), "reset-to-origin")
        self.assertFalse((self.vault / "local.md").exists())
        self.assertTrue((self.vault / "remote.md").exists())

    def test_sync_replaces_untracked_file_that_origin_now_tracks(self):
        other = self._other_clone()
        (other / "conflict.md").write_text("origin version\n", encoding="utf-8")
        self.git(other, "add", "conflict.md")
        self.git(other, "commit", "-m", "Remote conflict")
        self.git(other, "push")
        (self.vault / "conflict.md").write_text("local untracked\n", encoding="utf-8")
        self.assertEqual(sync_origin(self.vault), "reset-to-origin")
        self.assertEqual((self.vault / "conflict.md").read_text(encoding="utf-8"), "origin version\n")

    def test_pushes_only_note_preserves_staged_and_unstaged_changes(self):
        (self.vault / "private.txt").write_text("do not publish", encoding="utf-8")
        self.git(self.vault, "add", "private.txt")
        (self.vault / "existing.md").write_text("unstaged change\n", encoding="utf-8")
        publish_note(self.vault, self.target)
        paths = self.git(self.remote, "ls-tree", "-r", "--name-only", "main").splitlines()
        self.assertEqual(paths, ["Telegram/note.md", "existing.md"])
        self.assertEqual(self.git(self.remote, "show", "main:existing.md"), "initial")
        self.assertEqual(self.git(self.vault, "diff", "--cached", "--name-only"), "private.txt")
        self.assertEqual((self.vault / "existing.md").read_text(encoding="utf-8"), "unstaged change\n")
        before = self.git(self.vault, "rev-parse", "HEAD")
        publish_note(self.vault, self.target)
        self.assertEqual(self.git(self.vault, "rev-parse", "HEAD"), before)

    def test_refuses_to_push_unrelated_local_commit(self):
        (self.vault / "private.txt").write_text("secret", encoding="utf-8")
        self.git(self.vault, "add", "private.txt")
        self.git(self.vault, "commit", "-m", "Unrelated work")
        remote_before = self.git(self.remote, "rev-parse", "main")
        with self.assertRaisesRegex(RuntimeError, "посторонние"):
            publish_note(self.vault, self.target)
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), remote_before)

    def test_existing_staged_version_of_note_is_not_discarded(self):
        self.git(self.vault, "add", "Telegram/note.md")
        self.target.write_text("different working version\n", encoding="utf-8")
        before = self.git(self.vault, "show", ":Telegram/note.md")
        with self.assertRaisesRegex(RuntimeError, "staged"):
            publish_note(self.vault, self.target)
        self.assertEqual(self.git(self.vault, "show", ":Telegram/note.md"), before)

    def test_failed_push_can_be_retried_without_extra_commit(self):
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8", newline="\n")
        hook.chmod(0o755)
        with self.assertRaises(RuntimeError):
            publish_note(self.vault, self.target)
        pending = self.git(self.vault, "rev-parse", "HEAD")
        hook.unlink()
        publish_note(self.vault, self.target)
        self.assertEqual(self.git(self.vault, "rev-parse", "HEAD"), pending)
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), pending)

    def test_remote_advance_never_forces_or_rebases(self):
        other = self.root / "other"
        self.git(self.root, "clone", "-b", "main", str(self.remote), str(other))
        (other / "remote.md").write_text("remote update", encoding="utf-8")
        self.git(other, "add", "remote.md")
        self.git(other, "commit", "-m", "Remote update")
        self.git(other, "push")
        before = self.git(self.vault, "rev-parse", "HEAD")
        with self.assertRaises(RuntimeError):
            publish_note(self.vault, self.target)
        self.assertEqual(self.git(self.vault, "rev-parse", "HEAD"), before)
        self.assertTrue(self.target.exists())


if __name__ == "__main__":
    unittest.main()
