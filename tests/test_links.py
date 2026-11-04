"""Проверки тегов и связности на временной БД; без .env и реального LLM."""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from links import (
    Candidate,
    candidate_pairs,
    classify_pair,
    parse_link,
    record_link_failure,
    related,
    save_link,
)
from tags import extract_tags, normalize_tag


class TagTests(unittest.TestCase):
    def test_frontmatter_and_inline_tags(self):
        text = """---
title: Заметка
tags: [SQL, #MSSQL]
tag:
  - Linux
  - "OpenWRT"
---
# Заголовок не тег
Текст про #sql/waits и #OpenWRT.
`#код` и ```#fence``` не считаются.
[ссылка](#anchor) и [[заметка#раздел]] тоже.
"""
        self.assertEqual(
            extract_tags(text),
            ["linux", "mssql", "openwrt", "sql", "sql/waits"],
        )

    def test_normalize_tag(self):
        self.assertEqual(normalize_tag(" #SQL/WAITS "), "sql/waits")
        self.assertEqual(normalize_tag("#123"), "")
        self.assertEqual(normalize_tag("плохой тег"), "")

    def test_no_frontmatter(self):
        self.assertEqual(extract_tags("просто текст #тег"), ["тег"])

    def test_yaml_comments_and_quoted_hashtags(self):
        self.assertEqual(extract_tags("---\ntags: [SQL, linux] # тема\n---\n"), ["linux", "sql"])
        self.assertEqual(extract_tags('---\ntags: "#SQL #Linux"\n---\n'), ["linux", "sql"])


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        project = Path(__file__).resolve().parents[1]
        self.db = sqlite3.connect(Path(self.temp.name) / "brain.db")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript((project / "schema.sql").read_text(encoding="utf-8"))
        self.addCleanup(self.db.close)

    def add_note(self, path, tags=(), summary="саммари", keywords=(), entities=()):
        self.db.execute(
            "INSERT INTO notes(path,content_hash,mtime,title,summary,keywords,entities,questions,doc_type,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (path, "h", 0, path.rsplit("/", 1)[-1], summary,
             json.dumps(list(keywords), ensure_ascii=False),
             json.dumps(list(entities), ensure_ascii=False), "[]", "other", "now"),
        )
        for tag in tags:
            self.db.execute("INSERT INTO note_tags(path,tag) VALUES (?,?)", (path, tag))

    def test_candidates_by_shared_tags_and_skip_analyzed(self):
        self.add_note("v/a.md", ["sql"])
        self.add_note("v/b.md", ["sql"])
        self.add_note("v/c.md", ["sql"])
        pairs = candidate_pairs(self.db, max_per_note=5)
        self.assertEqual({(p.path_a, p.path_b) for p in pairs},
                         {("v/a.md", "v/b.md"), ("v/a.md", "v/c.md"), ("v/b.md", "v/c.md")})

        save_link(self.db, pairs[0], {"score": 0.9, "common": ["sql"], "reason": "ok"})
        self.db.commit()
        after = candidate_pairs(self.db, max_per_note=5)
        self.assertNotIn((pairs[0].path_a, pairs[0].path_b), {(p.path_a, p.path_b) for p in after})
        self.assertEqual(len(after), 2)

    def test_tag_fanout_and_per_note_limit(self):
        for name in "abc":
            self.add_note(f"v/{name}.md", ["общий"])
        self.assertEqual(candidate_pairs(self.db, max_tag_fanout=2), [])
        self.assertEqual(len(candidate_pairs(self.db, max_per_note=1)), 1)

    def test_fallback_by_shared_keywords_and_entities(self):
        self.add_note("v/a.md", keywords=["SQL", "wait stats"], entities=["MSSQL"])
        self.add_note("v/b.md", keywords=["sql", "Wait  stats"], entities=["mssql"])
        pairs = candidate_pairs(self.db, min_shared_terms=2)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].shared_tags, ())
        self.assertEqual(pairs[0].shared_terms, ("mssql", "sql", "wait stats"))
        save_link(self.db, pairs[0], {"score": 0.9, "common": ["SQL"], "reason": "ok"})
        self.db.commit()
        rows = related(self.db, "v/a.md", min_score=0)
        self.assertEqual(rows[0]["shared_tags"], [])
        self.assertEqual(rows[0]["shared_terms"], ["mssql", "sql", "wait stats"])

    def test_fallback_requires_min_shared_terms(self):
        self.add_note("v/a.md", keywords=["SQL"])
        self.add_note("v/b.md", keywords=["SQL"])
        self.assertEqual(candidate_pairs(self.db, min_shared_terms=2), [])
        self.assertEqual(len(candidate_pairs(self.db, min_shared_terms=1)), 1)
        self.assertEqual(candidate_pairs(self.db, min_shared_terms=0), [])

    def test_fallback_not_used_when_tag_candidate_exists(self):
        self.add_note("v/a.md", ["sql"], keywords=["SQL", "waits"])
        self.add_note("v/b.md", ["sql"], keywords=["SQL", "waits"])
        self.add_note("v/c.md", keywords=["SQL", "waits"])
        pairs = candidate_pairs(self.db)
        self.assertEqual({(p.path_a, p.path_b) for p in pairs}, {("v/a.md", "v/b.md")})

    def test_parse_link_accepts_wrapped_json_and_scale(self):
        result = parse_link('Ответ: {"score": 8, "common": "тема", "reason": "похоже"}')
        self.assertEqual(result["score"], 0.8)
        self.assertEqual(result["common"], ["тема"])

    def test_classify_pair_sends_summaries(self):
        calls = {}

        def create(**kwargs):
            calls.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(
                    content='{"score": 0.9, "common": ["sql"], "reason": "обе про ожидания"}'
                ))]
            )

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        result = classify_pair(
            client, "test-model",
            {"title": "A", "summary": "про SQL", "tags": ["sql"]},
            {"title": "B", "summary": "тоже про SQL", "tags": ["sql"]},
            ("sql",),
            shared_terms=("wait stats",),
        )
        self.assertEqual(result["score"], 0.9)
        self.assertEqual(calls["model"], "test-model")
        self.assertIn("про SQL", calls["messages"][1]["content"])
        self.assertIn("Общие теги: sql", calls["messages"][1]["content"])
        self.assertIn("Общие ключевые слова/сущности: wait stats", calls["messages"][1]["content"])

    def test_classify_pair_retries_truncated_answer_with_more_tokens(self):
        answers = [
            SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content='{"score": 0.5, "common": ["про'),
                finish_reason="length",
            )]),
            SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content='{"score": 0.7, "common": ["sql"], "reason": "ok"}'),
                finish_reason="stop",
            )]),
        ]
        calls = []

        def create(**kwargs):
            calls.append(kwargs)
            return answers[len(calls) - 1]

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.dict(
            os.environ, {"LLM_LINK_RETRIES": "1", "LLM_LINK_MAX_TOKENS_CAP": "4000"}
        ):
            result = classify_pair(
                client, "test-model",
                {"title": "A", "summary": "про SQL", "tags": ["sql"]},
                {"title": "B", "summary": "тоже про SQL", "tags": ["sql"]},
                ("sql",),
            )
        self.assertEqual(result["score"], 0.7)
        self.assertEqual(len(calls), 2)
        self.assertGreater(calls[1]["max_tokens"], calls[0]["max_tokens"])

    def test_classify_pair_reports_empty_answer(self):
        response = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=""),
            finish_reason="stop",
        )])

        def create(**kwargs):
            return response

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with (
            patch.dict(os.environ, {"LLM_LINK_RETRIES": "0"}),
            self.assertRaisesRegex(ValueError, "пустой ответ"),
        ):
            classify_pair(
                client, "test-model",
                {"title": "A", "summary": "s"},
                {"title": "B", "summary": "s"},
                (),
            )

    def test_failed_pairs_are_skipped_after_max_attempts(self):
        self.add_note("v/a.md", ["sql"])
        self.add_note("v/b.md", ["sql"])
        record_link_failure(self.db, "v/a.md", "v/b.md", "обрезано")
        self.db.commit()
        self.assertEqual(len(candidate_pairs(self.db, max_attempts=2)), 1)
        record_link_failure(self.db, "v/a.md", "v/b.md", "обрезано")
        record_link_failure(self.db, "v/a.md", "v/b.md", "обрезано")
        self.db.commit()
        self.assertEqual(candidate_pairs(self.db, max_attempts=2), [])
        # Без ограничения пара снова доступна (например после смены модели).
        self.assertEqual(len(candidate_pairs(self.db, max_attempts=0)), 1)

    def test_save_link_clears_failure_counter(self):
        self.add_note("v/a.md", ["sql"])
        self.add_note("v/b.md", ["sql"])
        record_link_failure(self.db, "v/a.md", "v/b.md", "обрезано")
        save_link(
            self.db,
            Candidate("v/a.md", "v/b.md", ("sql",), 1.0),
            {"score": 0.8, "common": ["sql"], "reason": "ok"},
        )
        self.db.commit()
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM link_failures").fetchone()[0], 0
        )

    def test_related_filters_and_orders(self):
        self.add_note("v/a.md", ["sql"])
        self.add_note("v/b.md", ["sql"])
        self.add_note("v/c.md", ["sql"])
        save_link(self.db, Candidate("v/a.md", "v/b.md", ("sql",), 1.0),
                  {"score": 0.4, "common": [], "reason": "слабо"})
        save_link(self.db, Candidate("v/a.md", "v/c.md", ("sql",), 1.0),
                  {"score": 0.9, "common": ["sql"], "reason": "сильно"})
        self.db.commit()
        rows = related(self.db, "v/a.md", min_score=0.65)
        self.assertEqual([row["path"] for row in rows], ["v/c.md"])
        self.assertEqual(rows[0]["shared_tags"], ["sql"])
        self.assertEqual(json.loads(json.dumps(rows[0]["common"])), ["sql"])


class SyncTagsTests(unittest.TestCase):
    def test_backfills_tags_and_fts_for_old_note(self):
        project = Path(__file__).resolve().parents[1]
        common = ModuleType("common")
        common.EXCLUDE_DIRS = set()
        common.VAULTS = []
        common.connect = lambda: None
        common.iter_notes = lambda: []
        common.stem_text = str.lower
        spec = importlib.util.spec_from_file_location("sync_under_test", project / "sync.py")
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"common": common}):
            spec.loader.exec_module(module)

        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript((project / "schema.sql").read_text(encoding="utf-8"))
        self.addCleanup(db.close)
        cursor = db.execute(
            "INSERT INTO notes(path,content_hash,mtime,title,summary,keywords,entities,questions,doc_type,updated_at)"
            " VALUES ('v/a.md','h',0,'A','s','[\"sql\"]','[\"mssql\"]','[]','other','now')"
        )
        db.execute(
            "INSERT INTO fts_notes(rowid,title,summary,keywords,questions) VALUES (?,?,?,?,?)",
            (cursor.lastrowid, "a", "s", "sql mssql", ""),
        )
        self.assertEqual(module.sync_note_tags(db, {"v/a.md": ["linux", "sql"]}), 1)
        self.assertEqual(module.sync_note_tags(db, {"v/a.md": ["linux", "sql"]}), 0)
        tags = [row[0] for row in db.execute("SELECT tag FROM note_tags ORDER BY tag")]
        self.assertEqual(tags, ["linux", "sql"])
        fts = db.execute("SELECT keywords FROM fts_notes").fetchone()[0]
        self.assertIn("linux", fts)


if __name__ == "__main__":
    unittest.main()
