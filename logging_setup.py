"""Ротирующий файловый журнал ошибок для посмертной диагностики.

По умолчанию (``LOG_FILE=auto``) предупреждения и ошибки пишутся в
``errors.log`` рядом с базой данных (`data/` доступен сервису на запись —
под systemd это ReadWritePaths). ``LOG_FILE=off`` оставляет логи только в
stdout/stderr (journald); путь (лучше абсолютный) переопределяет расположение.

В файл попадают только записи уровня ``LOG_FILE_LEVEL`` и выше, поэтому журнал
остаётся компактным списком сбоев, а не копией INFO-лога. Содержимое заметок и
прочие недоверенные данные сюда не пишутся; в сообщениях ошибок только стадии
и типы исключений (см. README, «Безопасность»).
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

logger = logging.getLogger(__name__)

# Компактный журнал: хватает истории на несколько дней сбоев.
_MAX_BYTES = 5 * 1024 * 1024
_BACKUPS = 3

# Значения, кроме документированного «off», означающие «файл не писать».
_DISABLED = {"", "off", "none", "0", "false"}


def resolve_log_file() -> Path | None:
    """Путь журнала ошибок; None, если файловый лог отключён."""
    raw = (os.environ.get("LOG_FILE", "auto") or "").strip()
    if raw.lower() in _DISABLED:
        return None
    if raw.lower() != "auto":
        return Path(os.path.expandvars(os.path.expanduser(raw)))
    # Ленивый импорт: common при старте валидирует VAULT_PATH, а журналу это
    # нужно только когда включён режим auto.
    from common import DB_PATH
    return DB_PATH.parent / "errors.log"


def _log_level() -> int:
    """Разобрать LOG_FILE_LEVEL; пустая — WARNING, неизвестная — WARNING и предупреждение."""
    name = (os.environ.get("LOG_FILE_LEVEL") or "WARNING").strip().upper()
    value = logging.getLevelNamesMapping().get(name)
    if isinstance(value, int):
        return value
    logger.warning("Неизвестный LOG_FILE_LEVEL %r; журнал пишет WARNING и выше", name)
    return logging.WARNING


def setup_file_logging() -> Path | None:
    """Подключить ротирующий обработчик к корневому логгеру; идемпотентно."""
    path = resolve_log_file()
    if path is None:
        return None

    root = logging.getLogger()
    for handler in root.handlers:
        if getattr(handler, "_brain_log_file", None) == path:
            return path

    level = _log_level()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUPS,
            encoding="utf-8",
            delay=True,
        )
    except (OSError, ValueError) as exc:
        logger.warning("Не удалось открыть LOG_FILE %s: %s", path, exc)
        return None

    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )
    handler._brain_log_file = path  # type: ignore[attr-defined]
    root.addHandler(handler)
    logger.info(
        "Журнал ошибок: %s (уровень %s, ротация %s MiB x %s)",
        path,
        logging.getLevelName(level),
        _MAX_BYTES // (1024 * 1024),
        _BACKUPS,
    )
    return path
