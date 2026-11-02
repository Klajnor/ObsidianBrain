"""Создание новых Telegram-заметок без изменения существующих файлов."""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import closing
from datetime import datetime, timezone

from common import VAULTS, connect, stem_text


def create_note(content: str, request_id: str) -> dict:
    """Создать заметку в первом хранилище и сразу добавить её в FTS, без LLM."""
    if os.environ.get("BRAIN_WRITE_ENABLED", "false").lower() != "true":
        raise ValueError("Запись выключена: задайте BRAIN_WRITE_ENABLED=true в окружении MCP-сервера")
    if not content.strip() or len(content) > 100_000:
        raise ValueError("Текст должен содержать от 1 до 100000 символов")
    if not request_id or len(request_id) > 200:
        raise ValueError("Нужен request_id длиной до 200 символов")
    # read_text и sync нормализуют переводы строк так же; хеш должен совпадать.
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    vault = VAULTS[0].resolve()
    folder = vault / "Telegram"
    folder.mkdir(exist_ok=True)
    if folder.is_symlink() or folder.resolve() != folder:
        raise ValueError("Telegram должен быть обычным каталогом внутри хранилища")
    key = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
    target = folder / f"{key}.md"
    path = f"{vault.name}/Telegram/{key}.md"
    text = content.strip() + "\n"
    created = False
    try:
        with target.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        created = True
    except FileExistsError:
        if target.is_symlink() or target.read_text(encoding="utf-8") != text:
            raise ValueError("Конфликт request_id: существующая заметка не изменена") from None

    title = content.strip().splitlines()[0].lstrip("# ")[:120] or "Telegram"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    # При сбое индексации новый файл остаётся: повтор безопасно достроит индекс,
    # а обычный sync подхватит его без потери текста.
    with closing(connect()) as db, db:
        existing = db.execute("SELECT content_hash FROM notes WHERE path=?", (path,)).fetchone()
        if existing is not None:
            if existing["content_hash"] != digest:
                raise ValueError("Заметка уже изменена: перезапись запрещена")
            return {"path": path, "created": False}
        cursor = db.execute(
            "INSERT INTO notes(path,content_hash,mtime,title,summary,keywords,entities,questions,doc_type,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (path, digest, target.stat().st_mtime, title, content[:1000],
             json.dumps(["Telegram"]), "[]", "[]", "clippings", datetime.now(timezone.utc).isoformat()),
        )
        db.execute("INSERT INTO fts_notes(rowid,title,summary,keywords,questions) VALUES (?,?,?,?,?)",
                   (cursor.lastrowid, stem_text(title), stem_text(content[:1000]), stem_text("Telegram"), ""))
        # Небольшие фрагменты сразу доступны через get_chunk.
        for start in range(0, len(text), 4000):
            chunk = text[start:start + 4000]
            cursor = db.execute("INSERT INTO chunks(path,heading,text,chunk_hash) VALUES (?,?,?,?)",
                                (path, title, chunk, hashlib.sha256(chunk.encode("utf-8")).hexdigest()))
            db.execute("INSERT INTO fts_chunks(rowid,heading,text) VALUES (?,?,?)",
                       (cursor.lastrowid, stem_text(title), stem_text(chunk)))
    return {"path": path, "created": created}
