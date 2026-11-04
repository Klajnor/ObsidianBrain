"""Каталог HTTPS credentials в юните и настройка helper без реальных секретов/сети."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


class PullCredentialsTests(unittest.TestCase):
    def test_pull_has_private_state_directory_and_keeps_hardening(self):
        lines = (PROJECT / "brain-pull.service").read_text(encoding="utf-8").splitlines()
        settings = dict(
            line.split("=", 1)
            for line in lines
            if "=" in line and not line.startswith("#")
        )
        self.assertEqual(settings["StateDirectory"], "brain-git")
        self.assertEqual(settings["StateDirectoryMode"], "0700")
        self.assertEqual(settings["ProtectSystem"], "strict")
        self.assertEqual(settings["ProtectHome"], "read-only")
        self.assertEqual(
            settings["ReadWritePaths"].split(),
            ["/opt/brain/data", "/srv/obsidian", "/srv/obsidian-personal"],
        )

    def test_local_helper_reset_uses_only_dedicated_store(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vault = root / "vault"
            vault.mkdir()
            credentials = root / "state" / "credentials"
            credentials.parent.mkdir()
            # Старая глобальная настройка указывает в недоступный каталог.
            old_credentials = root / "missing" / "credentials"
            global_config = root / "gitconfig"
            environment = dict(
                os.environ,
                GIT_CONFIG_NOSYSTEM="1",
                GIT_CONFIG_GLOBAL=str(global_config),
                GIT_TERMINAL_PROMPT="0",
            )

            def git(*args, text=None):
                return subprocess.run(
                    ["git", "-C", str(vault), *args],
                    env=environment, input=text, capture_output=True,
                    encoding="utf-8", timeout=30, check=True,
                )

            git("init")
            inherited = f'store --file="{old_credentials.as_posix()}"'
            git("config", "--global", "credential.helper", inherited)
            git("config", "--local", "--replace-all", "credential.helper", "")
            git("config", "--local", "--add", "credential.helper",
                f'store --file="{credentials.as_posix()}"')

            # Синтетические credentials: approve обращается только к локальному
            # helper, не соединяется с сервером и не читает настоящие секреты.
            result = git("credential", "approve", text=(
                "protocol=https\nhost=example.invalid\n"
                "username=test\npassword=synthetic-test-value\n\n"
            ))
            self.assertEqual(result.stderr, "")
            self.assertTrue(credentials.is_file())
            self.assertFalse(old_credentials.exists())
            self.assertEqual(
                git("config", "--global", "--get", "credential.helper").stdout.strip(),
                inherited,
            )


if __name__ == "__main__":
    unittest.main()
