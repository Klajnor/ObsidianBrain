"""Полная переиндексация, сброс связей и чистка «сирот» без реального LLM."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
SCHEMA = (PROJECT / "schema.sql").read_text(encoding="utf-8")


def _load_sync(vault: Path, db_path: Path, files: dict[str, Path], conns: list):
    """Загрузить sync.py с подменой хранилища и connect() на временную БД."""

    def connect():
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(SCHEMA)
        conns.append(db)
        return db

    common = ModuleType("common")
    common.VAULTS = [vault]
    common.EXCLUDE_DIRS = set()
    common.connect = connect
    common.stem_text = str.lower
    common.iter_notes = lambda: iter((path, rel) for rel, path in files.items())
    sys.modules["common"] = common
    spec = importlib.util.spec_from_file_location("sync_rebuild_under_test", PROJECT / "sync.py")
    try:
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop("common", None)


class RebuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.vault = root / "v"
        (self.vault / "n").mkdir(parents=True)
        self.files: dict[str, Path] = {}
        for name in ("a", "b"):
            path = self.vault / "n" / f"{name}.md"
            path.write_text(f"# {name}\n\nтекст {name} #shared\n", encoding="utf-8")
            self.files[f"v/n/{name}.md"] = path
        self.conns: list = []
        self.module = _load_sync(self.vault, root / "brain.db", self.files, self.conns)
        self.db = self.module.connect()
        self.addCleanup(lambda: [db.close() for db in self.conns])
        self._seed()

    def _seed(self):
        """Индекс, совпадающий с файлами: обычный sync не должен звать LLM."""
        with self.db:
            for rel, path in self.files.items():
                name = path.stem
                text = path.read_text(encoding="utf-8")
                cursor = self.db.execute(
                    "INSERT INTO notes(path,content_hash,mtime,title,summary,keywords,"
                    "entities,questions,doc_type,updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        rel, self.module.sha(text), 0.0, name, f"старое {name}",
                        json.dumps(["k"]), json.dumps([]), json.dumps([]),
                        "other", "2026-01-01 00:00:00",
                    ),
                )
                self.db.execute(
                    "INSERT INTO fts_notes(rowid,title,summary,keywords,questions)"
                    " VALUES (?,?,?,?,?)",
                    (cursor.lastrowid, name, f"старое {name}", "k", ""),
                )
                chunk = self.db.execute(
                    "INSERT INTO chunks(path,heading,text,chunk_hash) VALUES (?,?,?,?)",
                    (rel, "h", f"текст {name}", "x"),
                )
                self.db.execute(
                    "INSERT INTO fts_chunks(rowid,heading,text) VALUES (?,?,?)",
                    (chunk.lastrowid, "h", f"текст {name}"),
                )
                self.db.execute(
                    "INSERT INTO note_tags(path,tag) VALUES (?,?)", (rel, "shared")
                )
            self.db.execute(
                "INSERT INTO note_links(path_a,path_b,score,shared_tags,shared_terms,"
                "common_themes,reason,model,updated_at)"
                " VALUES ('v/n/a.md','v/n/b.md',0.9,'[\"shared\"]','[]','[]','ok','m','now')"
            )
            self.db.execute(
                "INSERT INTO link_failures(path_a,path_b,attempts,last_error,updated_at)"
                " VALUES ('v/n/a.md','v/n/b.md',2,'обрезано','now')"
            )

    def test_incremental_sync_does_not_call_llm_for_unchanged_notes(self):
        with (
            contextlib.redirect_stdout(io.StringIO()) as out,
            contextlib.redirect_stderr(io.StringIO()),
            patch.object(self.module, "distill", side_effect=AssertionError("LLM не должен вызываться")),
            patch.object(self.module, "build_links", return_value=0),
        ):
            self.assertEqual(self.module.sync(), 0)
        self.assertIn("к обработке: 0", out.getvalue())

    def test_reindex_reprocesses_unchanged_notes_and_invalidates_links(self):
        calls = []

        def card(title, text, chunks):
            calls.append(title)
            return {
                "summary": f"новое {title}",
                "keywords": ["k"],
                "entities": [],
                "questions": [],
                "doc_type": "other",
            }

        with (
            contextlib.redirect_stdout(io.StringIO()) as out,
            contextlib.redirect_stderr(io.StringIO()),
            patch.object(self.module, "distill", side_effect=card),
            patch.object(self.module, "build_links", return_value=0),
        ):
            self.assertEqual(self.module.sync(reindex=True), 0)
        self.assertEqual(sorted(calls), ["a", "b"])
        self.assertIn("--reindex: все заметки", out.getvalue())
        summaries = [
            row[0] for row in self.db.execute("SELECT summary FROM notes ORDER BY path")
        ]
        self.assertEqual(summaries, ["новое a", "новое b"])
        # index_note удаляет прежнюю заметку вместе с её связями.
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM note_links").fetchone()[0], 0
        )

    def test_rebuild_links_clears_scores_and_failure_counters(self):
        with (
            contextlib.redirect_stdout(io.StringIO()) as out,
            contextlib.redirect_stderr(io.StringIO()),
            patch.object(self.module, "distill", side_effect=AssertionError("LLM не должен вызываться")),
            patch.object(self.module, "build_links", return_value=0),
        ):
            self.assertEqual(self.module.sync(rebuild_links=True), 0)
        self.assertIn("кэш очищен", out.getvalue())
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM note_links").fetchone()[0], 0
        )
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM link_failures").fetchone()[0], 0
        )

    def test_prune_orphans_removes_deleted_note_from_all_indexes(self):
        gone = "v/n/gone.md"
        self.db.commit()
        self.db.execute("PRAGMA foreign_keys=OFF")  # имитируем старую БД с «сиротами»
        with self.db:
            self.db.execute(
                "INSERT INTO fts_notes(rowid,title,summary,keywords,questions)"
                " VALUES (987654,'gone','s','','')"
            )
            chunk = self.db.execute(
                "INSERT INTO chunks(path,heading,text,chunk_hash) VALUES (?,?,?,?)",
                (gone, "h", "t", "x"),
            )
            self.db.execute(
                "INSERT INTO fts_chunks(rowid,heading,text) VALUES (?,?,?)",
                (chunk.lastrowid, "h", "t"),
            )
            self.db.execute(
                "INSERT INTO fts_chunks(rowid,heading,text) VALUES (999999,'h','t')"
            )
            self.db.execute(
                "INSERT INTO note_tags(path,tag) VALUES (?,?)", (gone, "orphan")
            )
            self.db.execute(
                "INSERT INTO note_links(path_a,path_b,score,shared_tags,shared_terms,"
                "common_themes,reason,model,updated_at)"
                " VALUES ('v/n/a.md',?,0.9,'[]','[]','[]','r','m','now')",
                (gone,),
            )
            self.db.execute(
                "INSERT INTO link_failures(path_a,path_b,attempts,last_error,updated_at)"
                " VALUES ('v/n/a.md',?,1,'e','now')",
                (gone,),
            )
        self.db.execute("PRAGMA foreign_keys=ON")

        removed = self.module.prune_orphans(self.db)
        self.db.commit()
        self.assertGreater(removed, 0)
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM chunks WHERE path=?", (gone,)).fetchone()[0], 0
        )
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM note_tags WHERE path=?", (gone,)).fetchone()[0], 0
        )
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM note_links WHERE path_a=? OR path_b=?", (gone, gone)
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM link_failures WHERE path_a=? OR path_b=?", (gone, gone)
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM fts_notes WHERE rowid=987654").fetchone()[0], 0
        )
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM fts_chunks WHERE rowid=999999").fetchone()[0], 0
        )
        # Живые строка заметки, её тег и связь a–b не тронуты.
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 2
        )
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM note_tags").fetchone()[0], 2
        )
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM note_links").fetchone()[0], 1
        )

    def test_remove_note_clears_all_indexes_without_cascade(self):
        self.db.execute("PRAGMA foreign_keys=OFF")
        self.module.remove_note(self.db, "v/n/a.md")
        self.db.commit()
        self.db.execute("PRAGMA foreign_keys=ON")
        path = "v/n/a.md"
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM notes WHERE path=?", (path,)).fetchone()[0], 0
        )
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM chunks WHERE path=?", (path,)).fetchone()[0], 0
        )
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM note_tags WHERE path=?", (path,)).fetchone()[0], 0
        )
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM note_links WHERE path_a=? OR path_b=?", (path, path)
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM link_failures WHERE path_a=? OR path_b=?", (path, path)
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM fts_notes WHERE rowid NOT IN (SELECT rowid FROM notes)"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM fts_chunks WHERE rowid NOT IN (SELECT id FROM chunks)"
            ).fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
