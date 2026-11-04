"""Передача метаданных в MCP-канале записи без реального хранилища/LLM."""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch


class McpCaptureTests(unittest.TestCase):
    def load_server(self, enabled: bool):
        common = ModuleType("common")
        common.EXCLUDE_DIRS = set()
        common.EXCLUDE_FILES = set()
        common.VAULTS = []
        common.connect = Mock()
        common.fts_query = Mock()
        links = ModuleType("links")
        links.related = Mock()
        links.candidate_pairs = Mock(return_value=[])
        capture = ModuleType("capture")
        capture.WriteError = type("WriteError", (RuntimeError,), {})
        capture.create_note = Mock(return_value={"git_pushed": True})
        mcp_package = ModuleType("mcp")
        mcp_server_package = ModuleType("mcp.server")
        mcp_server_module = ModuleType("mcp.server.mcpserver")

        class FakeMCP:
            def __init__(self, name):
                self.tools = {}

            def tool(self):
                def register(function):
                    self.tools[function.__name__] = function
                    return function
                return register

        mcp_server_module.MCPServer = FakeMCP
        project = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("mcp_capture_under_test", project / "mcp_server.py")
        server = importlib.util.module_from_spec(spec)
        with (
            patch.dict(sys.modules, {"common": common, "links": links, "capture": capture,
                                     "mcp": mcp_package, "mcp.server": mcp_server_package,
                                     "mcp.server.mcpserver": mcp_server_module}),
            patch.dict(os.environ, {"BRAIN_WRITE_ENABLED": str(enabled).lower()}),
        ):
            spec.loader.exec_module(server)
        return server, capture

    def test_metadata_is_forwarded_without_changing_content_or_request_id(self):
        server, capture = self.load_server(True)
        result = server.create_note("**Исходный** текст", "tg:42:10", title="ИмяЗаметки", tags=["sql", "backup"])
        capture.create_note.assert_called_once_with("**Исходный** текст", "tg:42:10",
                                                  title="ИмяЗаметки", tags=["sql", "backup"])
        self.assertTrue(result["git_pushed"])

    def test_old_client_can_omit_metadata(self):
        server, capture = self.load_server(True)
        server.create_note("Текст", "id")
        capture.create_note.assert_called_once_with("Текст", "id", title="", tags=None)

    def test_disabled_write_tool_is_not_registered(self):
        server, capture = self.load_server(False)
        self.assertNotIn("create_note", server.mcp.tools)
        capture.create_note.assert_not_called()

    def test_metadata_validation_error_is_safe_and_not_success(self):
        server, capture = self.load_server(True)
        capture.create_note.side_effect = ValueError("private-note secret-token")
        result = server.create_note("Текст", "id", title="Имя", tags=["sql"])
        self.assertFalse(result["git_pushed"])
        self.assertEqual(result["error"], "stage=validate: ValueError")


if __name__ == "__main__":
    unittest.main()
