"""Dry-run: строка «связи: кандидатов N» по временным БД и хранилищу, без LLM."""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]


def _load_sync(vault: Path, db_path: Path, files: list[tuple[str, Path]], conns: list):
    """Загрузить sync.py с подменой хранилищ и connect() на тестовые."""
    schema = (PROJECT / "schema.sql").read_text(encoding="utf-8")

    def connect():
        import sqlite3
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(schema)
        conns.append(db)
        return db

    common = ModuleType("common")
    common.VAULTS = [vault]
    common.EXCLUDE_DIRS = set()
    common.connect = connect
    common.stem_text = str.lower
    common.iter_notes = lambda: iter([(path, rel) for rel, path in files])
    sys.modules["common"] = common
    spec = importlib.util.spec_from_file_location("sync_under_test", PROJECT / "sync.py")
    try:
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop("common", None)


class DryRunLinksTests(unittest.TestCase):
    def _prepare(self, tag: str = "shared-tag"):
        """Временное хранилище из двух заметок и индекс с парой по общему тегу."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)  # LIFO: сначала закроются БД, потом каталог
        self._conns: list = []
        root = Path(temp.name)
        vault = root / "v"
        (vault / "n").mkdir(parents=True)
        files = {}
        for name in ("a", "b"):
            p = vault / "n" / f"{name}.md"
            p.write_text(f"# {name}\n\nтекст", encoding="utf-8")
            rel = f"v/n/{name}.md"
            files[rel] = p
            # Совпадает со стабильным содержимым ниже: заметка не попадёт в todo.
        module = _load_sync(vault, root / "brain.db", sorted(files.items()), self._conns)
        db = module.connect()
        with db:
            for i, (rel, path) in enumerate(sorted(files.items())):
                db.execute(
                    "INSERT INTO notes(path, content_hash, mtime, title, summary, keywords,"
                    " entities, questions, doc_type, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        rel, module.sha(path.read_text(encoding="utf-8")), 0.0, rel,
                        "сводка",
                        json.dumps([]), json.dumps([]), json.dumps([]),
                        "reference", "2026-01-0%d 00:00:00" % (i + 1),
                    ),
                )
        db.executemany(
            "INSERT INTO note_tags(path, tag) VALUES (?,?)",
            [(rel, tag) for rel in files],
        )
        db.commit()
        db.close()
        self.addCleanup(lambda: [c.close() for c in self._conns])
        return module, files

    def test_dry_run_shows_link_candidate_count_without_llm(self):
        module, files = self._prepare()
        # Хэши совпадают с индексом: todo пуст, кандидаты считаются по заметкам.
        with (
            patch.dict(os.environ, {"BRAIN_LINKS_ENABLED": "1"}),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            self.assertEqual(module.sync(dry_run=True), 0)
        text = out.getvalue()
        self.assertIn("связи: кандидатов 1", text)
        self.assertNotIn("сравню", text)  # LLM-сравнения не планируются

    def test_dry_run_without_enabled_links_prints_no_line(self):
        module, files = self._prepare()
        with (
            patch.dict(os.environ, {"BRAIN_LINKS_ENABLED": "0"}),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            self.assertEqual(module.sync(dry_run=True), 0)
        self.assertNotIn("связи:", out.getvalue())


if __name__ == "__main__":
    unittest.main()
