"""Проверки записи на временном хранилище, без .env и реальной БД."""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        project = Path(__file__).resolve().parents[1]
        schema = (project / "schema.sql").read_text(encoding="utf-8")

        def connect():
            db = sqlite3.connect(self.root / "brain.db")
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript(schema)
            return db

        common = ModuleType("common")
        common.VAULTS = [self.vault]
        common.connect = connect
        common.stem_text = str.lower
        spec = importlib.util.spec_from_file_location("capture_under_test", project / "capture.py")
        self.capture = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"common": common}):
            spec.loader.exec_module(self.capture)
        self.env = patch.dict(os.environ, {"BRAIN_WRITE_ENABLED": "true"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.connect = connect

    def test_create_search_and_idempotent_retry(self):
        result = self.capture.create_note("Космический проект\nДанные для поиска", "tg:42:1")
        self.assertTrue(result["created"])
        self.assertFalse(self.capture.create_note("Космический проект\nДанные для поиска", "tg:42:1")["created"])
        self.assertEqual(len(list((self.vault / "Telegram").glob("*.md"))), 1)
        db = self.connect()
        self.addCleanup(db.close)
        self.assertEqual(db.execute("SELECT count(*) FROM notes").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT count(*) FROM fts_chunks WHERE fts_chunks MATCH 'поиска'").fetchone()[0], 1)

    def test_conflict_never_overwrites_file(self):
        self.capture.create_note("original", "same")
        with self.assertRaises(ValueError):
            self.capture.create_note("changed", "same")
        target = next((self.vault / "Telegram").glob("*.md"))
        self.assertEqual(target.read_text(encoding="utf-8"), "original\n")

    def test_windows_newlines_have_stable_hash(self):
        self.capture.create_note("первая\r\nвторая", "windows")
        self.assertFalse(self.capture.create_note("первая\r\nвторая", "windows")["created"])
        target = next((self.vault / "Telegram").glob("*.md"))
        text = target.read_text(encoding="utf-8")
        import hashlib

        db = self.connect()
        self.addCleanup(db.close)
        self.assertEqual(db.execute("SELECT content_hash FROM notes").fetchone()[0],
                         hashlib.sha256(text.encode("utf-8")).hexdigest())

    def test_title_has_no_control_chars(self):
        self.capture.create_note("bad\x1b[31m title\nsecond", "ctrl")
        db = self.connect()
        self.addCleanup(db.close)
        title = db.execute("SELECT title FROM notes").fetchone()[0]
        self.assertNotIn("\x1b", title)
        self.assertNotIn("\n", title)
        self.assertTrue(title.strip())

    def test_disabled_and_invalid_input(self):
        with patch.dict(os.environ, {"BRAIN_WRITE_ENABLED": "false"}), self.assertRaises(ValueError):
            self.capture.create_note("data", "id")
        for content, key in [("", "id"), ("x" * 100001, "id"), ("text", "")]:
            with self.assertRaises(ValueError):
                self.capture.create_note(content, key)
        self.assertFalse((self.vault / "Telegram").exists())

    def test_index_failure_can_be_retried(self):
        with patch.object(self.capture, "connect", side_effect=sqlite3.OperationalError), self.assertRaises(sqlite3.OperationalError):
            self.capture.create_note("retained", "id")
        self.assertEqual(len(list((self.vault / "Telegram").glob("*.md"))), 1)
        self.capture.create_note("retained", "id")
        db = self.connect()
        self.addCleanup(db.close)
        self.assertEqual(db.execute("SELECT count(*) FROM notes").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
