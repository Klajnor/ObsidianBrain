"""Создание новых Telegram-заметок без изменения существующих файлов."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone

from common import VAULTS, connect, stem_text
from git_publish import publish_note, sync_origin
from tags import extract_tags

_WRITE_LOCK = threading.Lock()


class WriteError(RuntimeError):
    """Ошибка записи с безопасным кодом стадии для клиента."""

    def __init__(self, stage: str, detail: str = "") -> None:
        super().__init__(f"stage={stage}" + (f": {detail}" if detail else ""))


def create_note(content: str, request_id: str) -> dict:
    """Создать и индексировать заметку, затем закоммитить и отправить в Git."""
    with _WRITE_LOCK:
        return _create_note(content, request_id)


def _create_note(content: str, request_id: str) -> dict:
    if os.environ.get("BRAIN_WRITE_ENABLED", "false").lower() != "true":
        raise ValueError("Запись выключена: задайте BRAIN_WRITE_ENABLED=true в окружении MCP-сервера")
    if not content.strip() or len(content) > 100_000:
        raise ValueError("Текст должен содержать от 1 до 100000 символов")
    if not request_id or len(request_id) > 200:
        raise ValueError("Нужен request_id длиной до 200 символов")
    # read_text и sync нормализуют переводы строк так же; хеш должен совпадать.
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    vault = VAULTS[0].resolve()
    # Сначала выравниваем хранилище по origin: при расхождении origin главнее.
    try:
        sync_origin(vault)
    except Exception as exc:  # noqa: BLE001 - заметка не должна создаваться без sync
        raise WriteError("sync", f"{type(exc).__name__}: {exc}") from exc
    folder = vault / "Telegram"
    key = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
    target = folder / f"{key}.md"
    path = f"{vault.name}/Telegram/{key}.md"
    text = content.strip() + "\n"
    created = False
    try:
        folder.mkdir(exist_ok=True)
        if folder.is_symlink() or folder.resolve() != folder:
            raise ValueError("Telegram должен быть обычным каталогом внутри хранилища")
        with target.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        created = True
    except FileExistsError:
        try:
            if target.is_symlink() or target.read_text(encoding="utf-8") != text:
                raise ValueError("Конфликт request_id: существующая заметка не изменена") from None
        except OSError as exc:
            raise WriteError("file", type(exc).__name__) from exc
    except ValueError:
        raise
    except OSError as exc:
        raise WriteError("file", type(exc).__name__) from exc

    first_line = content.strip().splitlines()[0].lstrip("# ")
    # Текст приходит из сообщения: убираем управляющие символы (ANSI и т.п.),
    # чтобы заголовок не ломал терминал/журнал и не уезжал в поиск как есть.
    title = " ".join("".join(ch for ch in first_line if ch.isprintable()).split())[:120] or "Telegram"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    # При сбое индексации новый файл остаётся: повтор безопасно достроит индекс,
    # а обычный sync подхватит его без потери текста.
    try:
        with closing(connect()) as db, db:
            existing = db.execute("SELECT content_hash FROM notes WHERE path=?", (path,)).fetchone()
            if existing is not None:
                if existing["content_hash"] != digest:
                    raise ValueError("Заметка уже изменена: перезапись запрещена")
            else:
                _index_note(db, path, target, content, text, title, digest)
    except ValueError:
        raise
    except (sqlite3.Error, OSError) as exc:
        raise WriteError("index", type(exc).__name__) from exc
    # Даже для уже существующей заметки повторяем push: прошлый мог не удаться.
    try:
        publish_note(vault, target)
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001 - стадия должна дойти до клиента
        raise WriteError("git", type(exc).__name__) from exc
    return {"path": path, "created": created, "git_pushed": True}


def _index_note(db, path, target, content, text, title, digest):
    """Индексировать новый файл; гонка с sync.py не должна ломать запись."""
    tags = extract_tags(text)
    try:
        cursor = db.execute(
            "INSERT INTO notes(path,content_hash,mtime,title,summary,keywords,entities,questions,doc_type,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (path, digest, target.stat().st_mtime, title, content[:1000],
             json.dumps(["Telegram"]), "[]", "[]", "clippings", datetime.now(timezone.utc).isoformat()),
        )
    except sqlite3.IntegrityError:
        # sync.py (таймер) мог проиндексировать файл между проверкой и вставкой.
        row = db.execute("SELECT content_hash FROM notes WHERE path=?", (path,)).fetchone()
        if row is not None and row["content_hash"] == digest:
            return
        raise ValueError("Конфликт request_id: заметка уже проиндексирована с другим содержимым") from None
    db.execute("INSERT INTO fts_notes(rowid,title,summary,keywords,questions) VALUES (?,?,?,?,?)",
               (cursor.lastrowid, stem_text(title), stem_text(content[:1000]),
                stem_text(" ".join(["Telegram"] + tags)), ""))
    for tag in tags:
        db.execute("INSERT OR IGNORE INTO note_tags(path, tag) VALUES (?,?)", (path, tag))
    # Небольшие фрагменты сразу доступны через get_chunk.
    for start in range(0, len(text), 4000):
        chunk = text[start:start + 4000]
        cursor = db.execute("INSERT INTO chunks(path,heading,text,chunk_hash) VALUES (?,?,?,?)",
                            (path, title, chunk, hashlib.sha256(chunk.encode("utf-8")).hexdigest()))
        db.execute("INSERT INTO fts_chunks(rowid,heading,text) VALUES (?,?,?)",
                   (cursor.lastrowid, stem_text(title), stem_text(chunk)))
