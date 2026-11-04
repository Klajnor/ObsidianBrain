"""Проверки записи на временном хранилище, без .env и реальной БД."""

from __future__ import annotations

import hashlib
import importlib.util
import json
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
        publisher = patch.object(self.capture, "publish_note")
        self.publish = publisher.start()
        self.addCleanup(publisher.stop)
        syncer = patch.object(self.capture, "sync_origin", return_value="up-to-date")
        self.sync = syncer.start()
        self.addCleanup(syncer.stop)
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

    def test_create_indexes_inline_tags(self):
        self.capture.create_note("Заметка #SQL про #MSSQL", "tags")
        db = self.connect()
        self.addCleanup(db.close)
        tags = [row[0] for row in db.execute("SELECT tag FROM note_tags ORDER BY tag")]
        self.assertEqual(tags, ["mssql", "sql"])

    def test_concurrent_sync_index_is_idempotent(self):
        result = self.capture.create_note("race", "race")
        target = next((self.vault / "Telegram").glob("*.md"))
        text = target.read_text(encoding="utf-8")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        db = self.connect()
        self.addCleanup(db.close)
        before = tuple(
            db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("notes", "fts_notes", "chunks", "fts_chunks")
        )
        self.capture._index_note(db, result["path"], target, "race", text, "race", digest)
        db.commit()
        after = tuple(
            db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("notes", "fts_notes", "chunks", "fts_chunks")
        )
        self.assertEqual(before, after)

    def test_sync_failure_does_not_create_note(self):
        self.sync.side_effect = RuntimeError("origin недоступен")
        with self.assertRaises(self.capture.WriteError) as caught:
            self.capture.create_note("not created", "sync-fail")
        self.assertIn("stage=sync", str(caught.exception))
        self.assertFalse((self.vault / "Telegram").exists())
        self.publish.assert_not_called()

    def test_conflict_never_overwrites_file(self):
        self.capture.create_note("original", "same")
        target = next((self.vault / "Telegram").glob("*.md"))
        original = target.read_bytes()
        with self.assertRaises(ValueError):
            self.capture.create_note("changed", "same")
        self.assertEqual(target.read_bytes(), original)

    def test_readable_name_is_sanitized_and_fits_48_chars_with_suffix(self):
        result = self.capture.create_note("Точный **текст**\nВторая строка", "name",
                                          title="../Е\u0308жики и SQL / 中文 🙂 42 " * 10, tags=["sql"])
        target = self.vault / "Telegram" / Path(result["path"]).name
        self.assertRegex(target.stem, r"\A[A-Za-zА-Яа-яЁё0-9]{1,48}\Z")
        self.assertTrue(target.stem.startswith("ЁжикиИSQL42"))
        self.assertTrue(target.read_text(encoding="utf-8").endswith("\n\nТочный **текст**\nВторая строка\n"))

    def test_generated_tags_are_in_frontmatter_index_and_keywords(self):
        result = self.capture.create_note("Настройка базы #SQLite", "metadata",
                                          title="НастройкаSQLite", tags=["SQL", "backup", "sql"])
        target = self.vault / "Telegram" / Path(result["path"]).name
        text = target.read_text(encoding="utf-8")
        self.assertIn('tags: ["backup", "sql", "sqlite"]', text)
        self.assertEqual(result["tags"], ["backup", "sql", "sqlite"])
        db = self.connect()
        self.addCleanup(db.close)
        self.assertEqual([row[0] for row in db.execute("SELECT tag FROM note_tags ORDER BY tag")],
                         result["tags"])
        card = db.execute("SELECT title,keywords FROM notes").fetchone()
        self.assertEqual(card["title"], "НастройкаSQLite")
        self.assertIn("backup", json.loads(card["keywords"]))
        self.assertEqual(db.execute("SELECT count(*) FROM fts_notes WHERE fts_notes MATCH 'backup'").fetchone()[0], 1)

    def test_original_frontmatter_tags_are_preserved_without_rewriting_body(self):
        body = '---\ntags: ["old-tag"]\n---\n\nИсходный текст #inline'
        result = self.capture.create_note(body, "frontmatter", title="Текст", tags=["new-tag"])
        self.assertEqual(result["tags"], ["inline", "new-tag", "old-tag"])
        target = self.vault / "Telegram" / Path(result["path"]).name
        self.assertTrue(target.read_text(encoding="utf-8").endswith(body + "\n"))

    def test_same_title_different_actions_have_unique_files(self):
        first = self.capture.create_note("Один текст", "first", title="ОдинаковоеИмя")
        second = self.capture.create_note("Один текст", "second", title="ОдинаковоеИмя")
        self.assertNotEqual(first["path"], second["path"])
        self.assertEqual(len(list((self.vault / "Telegram").glob("*.md"))), 2)

    def test_changed_ai_metadata_on_retry_reuses_file_even_after_index_failure(self):
        with patch.object(self.capture, "connect", side_effect=sqlite3.OperationalError), self.assertRaises(self.capture.WriteError):
            self.capture.create_note("Исходный текст", "retry", title="ПервоеИмя", tags=["first"])
        target = next((self.vault / "Telegram").glob("*.md"))
        original = target.read_bytes()
        result = self.capture.create_note("Исходный текст", "retry", title="ДругоеИмя", tags=["second"])
        self.assertFalse(result["created"])
        self.assertEqual(result["title"], "ПервоеИмя")
        self.assertEqual(result["tags"], ["first"])
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(len(list((self.vault / "Telegram").glob("*.md"))), 1)

    def test_short_hash_collision_uses_counter_without_overwrite(self):
        # Укорачиваем суффикс, чтобы детерминированно проверить настоящую коллизию.
        seen = {}
        for i in range(17):
            key = f"collision:{i}"
            suffix = hashlib.sha256(key.encode()).hexdigest()[0]
            if suffix in seen:
                first_key, second_key = seen[suffix], key
                break
            seen[suffix] = key
        with patch.object(self.capture, "_KEY_SUFFIX", 1):
            first = self.capture.create_note("Первый текст", first_key, title="Имя" * 20)
            second = self.capture.create_note("Второй текст", second_key, title="Имя" * 20)
            retry = self.capture.create_note("Второй текст", second_key, title="ДругоеИмя")
        self.assertNotEqual(first["path"], second["path"])
        self.assertEqual(retry["path"], second["path"])
        self.assertFalse(retry["created"])
        self.assertLessEqual(len(Path(second["path"]).stem), 48)

    def test_occupied_name_is_not_overwritten(self):
        folder = self.vault / "Telegram"
        folder.mkdir()
        suffix = hashlib.sha256(b"occupied").hexdigest()[:12]
        occupied = folder / f"Имя{suffix}.md"
        occupied.write_text("Чужая заметка", encoding="utf-8")
        result = self.capture.create_note("Новый текст", "occupied", title="Имя")
        self.assertNotEqual(Path(result["path"]).name, occupied.name)
        self.assertEqual(occupied.read_text(encoding="utf-8"), "Чужая заметка")
        self.assertTrue(Path(result["path"]).stem.endswith("1" + suffix))

    def test_legacy_request_is_retried_without_renaming_or_adding_tags(self):
        folder = self.vault / "Telegram"
        folder.mkdir()
        target = folder / (hashlib.sha256(b"legacy").hexdigest() + ".md")
        target.write_text("Старая заметка\n", encoding="utf-8")
        original = target.read_bytes()
        result = self.capture.create_note("Старая заметка", "legacy", title="НовоеИмя", tags=["new"])
        self.assertFalse(result["created"])
        self.assertEqual(Path(result["path"]).name, target.name)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(len(list(folder.glob("*.md"))), 1)

    def test_invalid_metadata_is_rejected_before_sync_or_file_creation(self):
        for title, tags in ((42, []), ("Имя", "sql"), ("Имя", [42]), ("Имя", ["sql"] * 9)):
            with self.subTest(title=title, tags=tags), self.assertRaises(ValueError):
                self.capture.create_note("Текст", "id", title=title, tags=tags)
        self.sync.assert_not_called()
        self.assertFalse((self.vault / "Telegram").exists())

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
        with (
            patch.object(self.capture, "connect", side_effect=sqlite3.OperationalError),
            self.assertRaises(self.capture.WriteError) as caught,
        ):
            self.capture.create_note("retained", "id")
        self.assertIn("stage=index", str(caught.exception))
        self.assertEqual(len(list((self.vault / "Telegram").glob("*.md"))), 1)
        self.capture.create_note("retained", "id")
        db = self.connect()
        self.addCleanup(db.close)
        self.assertEqual(db.execute("SELECT count(*) FROM notes").fetchone()[0], 1)

    def test_push_failure_keeps_note_and_retry_publishes_without_duplicate(self):
        self.publish.side_effect = RuntimeError("push failed")
        with self.assertRaises(RuntimeError):
            self.capture.create_note("retained", "push-retry")
        self.assertEqual(len(list((self.vault / "Telegram").glob("*.md"))), 1)
        self.publish.side_effect = None
        result = self.capture.create_note("retained", "push-retry")
        self.assertFalse(result["created"])
        self.assertTrue(result["git_pushed"])
        self.assertEqual(self.publish.call_count, 2)


if __name__ == "__main__":
    unittest.main()
