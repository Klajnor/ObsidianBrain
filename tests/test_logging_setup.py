"""Журнал ошибок: выбор пути, фильтрация по уровню и идемпотентность."""
import logging
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import logging_setup


class ErrorLogSetupTests(unittest.TestCase):
    """Тесты не требуют .env: путь auto подменяется на временный каталог."""

    @contextmanager
    def _db_path(self, db: Path):
        if "common" in sys.modules:
            with patch.object(sys.modules["common"], "DB_PATH", db):
                yield
        else:
            stub = ModuleType("common")
            stub.DB_PATH = db
            with patch.dict("sys.modules", {"common": stub}):
                yield

    def _close_handlers(self) -> None:
        root = logging.getLogger()
        for handler in list(root.handlers):
            if getattr(handler, "_brain_log_file", None) is not None:
                root.removeHandler(handler)
                handler.close()

    def tearDown(self) -> None:
        self._close_handlers()

    def test_auto_writes_next_to_the_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                self._db_path(Path(tmp) / "brain.db"),
                patch.dict(os.environ, {"LOG_FILE": "auto", "LOG_FILE_LEVEL": "WARNING"}),
            ):
                path = logging_setup.setup_file_logging()
                self.assertEqual(path, Path(tmp) / "errors.log")
                logging.getLogger("test.error_log").warning("boom")
                for handler in logging.getLogger().handlers:
                    handler.flush()
                text = (Path(tmp) / "errors.log").read_text(encoding="utf-8")
                self._close_handlers()
        self.assertIn("boom", text)
        self.assertIn("WARNING", text)

    def test_off_and_disabled_values_switch_the_file_off(self):
        for value in ("off", "", "none", "0", "false", "OFF"):
            with patch.dict(os.environ, {"LOG_FILE": value}):
                self.assertIsNone(logging_setup.resolve_log_file(), value)
                self.assertIsNone(logging_setup.setup_file_logging(), value)

    def test_explicit_path_overrides_the_default_and_filters_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "custom.log"
            with patch.dict(os.environ, {"LOG_FILE": str(target), "LOG_FILE_LEVEL": "ERROR"}):
                path = logging_setup.setup_file_logging()
                self.assertEqual(path, target)
                logging.getLogger("test.error_log").warning("hidden")
                logging.getLogger("test.error_log").error("shown")
                for handler in logging.getLogger().handlers:
                    handler.flush()
                text = target.read_text(encoding="utf-8")
                self._close_handlers()
        self.assertIn("shown", text)
        self.assertNotIn("hidden", text)

    def test_setup_is_idempotent_and_unknown_level_falls_back_to_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "flat.log"
            with patch.dict(os.environ, {"LOG_FILE": str(target), "LOG_FILE_LEVEL": "MUSIC"}):
                first = logging_setup.setup_file_logging()
                second = logging_setup.setup_file_logging()
                self.assertEqual(first, second, target)
                handlers = [h for h in logging.getLogger().handlers
                            if getattr(h, "_brain_log_file", None) == target]
                self.assertEqual(len(handlers), 1)
                self.assertEqual(handlers[0].level, logging.WARNING)
                self._close_handlers()

    def test_unset_level_means_warning_silently(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "quiet.log"
            # LOG_FILE_LEVEL вообще не задан: fallback без лишних предупреждений.
            with patch.dict(os.environ, {"LOG_FILE": str(target)}, clear=False):
                os.environ.pop("LOG_FILE_LEVEL", None)
                path = logging_setup.setup_file_logging()
                self.assertEqual(path, target)
                handlers = [h for h in logging.getLogger().handlers
                            if getattr(h, "_brain_log_file", None) == target]
                self.assertEqual(len(handlers), 1)
                self.assertEqual(handlers[0].level, logging.WARNING)
                self._close_handlers()


if __name__ == "__main__":
    unittest.main()
