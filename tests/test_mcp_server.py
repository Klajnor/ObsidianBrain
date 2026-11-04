"""Проверки MCP-обвязки связей без запуска реального MCP-сервера."""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from links import Candidate, save_link


class McpRelatedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        project = Path(__file__).resolve().parents[1]
        self.db_path = self.root / "brain.db"
        self.schema = (project / "schema.sql").read_text(encoding="utf-8")
        self.open_dbs = []

        def connect(readonly=False):
            db = sqlite3.connect(self.db_path)
            db.row_factory = sqlite3.Row
            db.executescript(self.schema)
            self.open_dbs.append(db)
            return db

        self.connect = connect
        self.addCleanup(self._close_dbs)
        db = connect()
        db.execute(
            "INSERT INTO notes(path,content_hash,mtime,title,summary,keywords,entities,questions,doc_type,updated_at)"
            " VALUES ('v/a.md','h',0,'A','саммари A','[]','[]','[]','other','now')"
        )
        db.execute(
            "INSERT INTO notes(path,content_hash,mtime,title,summary,keywords,entities,questions,doc_type,updated_at)"
            " VALUES ('v/b.md','h',0,'B','саммари B','[]','[]','[]','other','now')"
        )
        db.execute("INSERT INTO note_tags(path,tag) VALUES ('v/a.md','sql'), ('v/b.md','sql')")
        rowid = db.execute("SELECT rowid FROM notes WHERE path='v/a.md'").fetchone()[0]
        db.execute(
            "INSERT INTO fts_notes(rowid,title,summary,keywords,questions) VALUES (?,?,?,?,?)",
            (rowid, "a", "саммари A", "", ""),
        )
        save_link(db, Candidate("v/a.md", "v/b.md", ("sql",), 1.0),
                  {"score": 0.9, "common": ["SQL"], "reason": "обе про SQL"})
        db.commit()
        db.close()

        common = ModuleType("common")
        common.EXCLUDE_DIRS = set()
        common.EXCLUDE_FILES = set()
        common.VAULTS = []
        common.connect = connect
        common.fts_query = lambda query: query
        mcp_package = ModuleType("mcp")
        mcp_server_package = ModuleType("mcp.server")
        mcp_server_module = ModuleType("mcp.server.mcpserver")

        class FakeMCP:
            def __init__(self, name):
                self.name = name

            def tool(self):
                return lambda function: function

            def run(self):
                pass

        mcp_server_module.MCPServer = FakeMCP
        spec = importlib.util.spec_from_file_location("mcp_under_test", project / "mcp_server.py")
        self.mcp = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {
            "common": common,
            "mcp": mcp_package,
            "mcp.server": mcp_server_package,
            "mcp.server.mcpserver": mcp_server_module,
        }):
            spec.loader.exec_module(self.mcp)

    def _close_dbs(self):
        for db in self.open_dbs:
            try:
                db.close()
            except sqlite3.Error:
                pass

    def test_get_related_returns_stored_link(self):
        rows = self.mcp.get_related("v/a.md")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["path"], "v/b.md")
        self.assertEqual(rows[0]["score"], 0.9)
        self.assertEqual(rows[0]["shared_tags"], ["sql"])
        self.assertEqual(rows[0]["summary"], "саммари B")

    def test_card_has_tags(self):
        db = self.connect()
        self.addCleanup(db.close)
        row = db.execute("SELECT * FROM notes WHERE path='v/a.md'").fetchone()
        self.assertEqual(self.mcp._card(db, row)["tags"], ["sql"])

    def test_search_includes_related_without_summary(self):
        rows = self.mcp.search_notes("саммари", k=1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["path"], "v/a.md")
        self.assertEqual(rows[0]["related"][0]["path"], "v/b.md")
        self.assertNotIn("summary", rows[0]["related"][0])

    def test_brain_status_returns_counts_only(self):
        status = self.mcp.brain_status()
        self.assertEqual(status["notes"], 2)
        self.assertEqual(status["chunks"], 0)
        self.assertEqual(status["tags"], 1)
        self.assertEqual(status["links"], 1)
        self.assertEqual(status["strong_links"], 1)
        self.assertEqual(status["pending_links"], 0)
        self.assertEqual(status["failed_links"], 0)
        self.assertTrue(status["links_enabled"])
        # No paths or note text may leak through the status tool.
        self.assertFalse(any("v/" in str(value) for value in status.values()))

    def test_brain_status_reports_postponed_pairs(self):
        db = self.connect()
        db.execute(
            "INSERT INTO link_failures(path_a,path_b,attempts,last_error,updated_at)"
            " VALUES ('v/a.md','v/b.md',3,'e','now')"
        )
        db.commit()
        db.close()
        status = self.mcp.brain_status()
        self.assertEqual(status["failed_links"], 1)


if __name__ == "__main__":
    unittest.main()
