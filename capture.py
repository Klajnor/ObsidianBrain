"""Создание новых Telegram-заметок без изменения существующих файлов."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import unicodedata
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from common import VAULTS, connect, stem_text
from git_publish import publish_note, sync_origin
from tags import extract_tags, normalize_tag

_WRITE_LOCK = threading.Lock()
_NAME_WORDS = re.compile(r"[A-Za-zА-Яа-яЁё0-9]+")
_MAX_NAME = 48
_KEY_SUFFIX = 12


class WriteError(RuntimeError):
    """Ошибка записи с безопасным кодом стадии для клиента."""

    def __init__(self, stage: str, detail: str = "") -> None:
        super().__init__(f"stage={stage}" + (f": {detail}" if detail else ""))


def create_note(content: str, request_id: str, title: str = "", tags: list[str] | None = None) -> dict:
    """Создать и индексировать заметку, затем закоммитить и отправить в Git."""
    with _WRITE_LOCK:
        return _create_note(content, request_id, title, tags)


def _clean_title(value: str) -> str:
    """Имя без расширения: только русские/латинские буквы и цифры."""
    value = unicodedata.normalize("NFC", value.strip())
    value = re.sub(r"\.md$", "", value, flags=re.IGNORECASE)
    return "".join(word[0].upper() + word[1:] for word in _NAME_WORDS.findall(value))[:_MAX_NAME] or "Заметка"


def _read_note(target: Path) -> str:
    """Не читать симлинки, специальные файлы и чрезмерно большие заметки."""
    if target.is_symlink() or target.resolve() != target or not target.is_file():
        raise ValueError("Заметка должна быть обычным файлом внутри Telegram")
    with target.open(encoding="utf-8") as stream:
        text = stream.read(1_000_001)
    if len(text) > 1_000_000:
        raise ValueError("Существующая заметка слишком велика")
    return text


def _find_existing(folder: Path, key: str, body: str) -> tuple[Path, str, str] | None:
    """Найти прошлую запись независимо от повторно сгенерированных имени/тегов."""
    legacy = folder / f"{key}.md"
    if legacy.exists() or legacy.is_symlink():
        text = _read_note(legacy)
        if text != body:
            raise ValueError("Конфликт request_id: существующая заметка не изменена")
        return legacy, text, _clean_title(body.splitlines()[0])
    prefix = f"---\nbrain_request_id: {key}\n"
    for target in sorted(folder.glob(f"*{key[:_KEY_SUFFIX]}.md")):
        # Совпадение короткого суффикса не означает совпадение request_id.
        if target.is_symlink() or not target.is_file():
            continue
        text = _read_note(target)
        if not text.startswith(prefix):
            continue
        header, separator, saved_body = text.partition("\n---\n\n")
        if not separator or saved_body != body:
            raise ValueError("Конфликт request_id: существующая заметка не изменена")
        title_line = header[len(prefix):].split("\n", 1)[0]
        if not title_line.startswith("title: "):
            raise ValueError("Метаданные существующей заметки изменены")
        title = json.loads(title_line[len("title: "):])
        if not isinstance(title, str):
            # Это повреждённые данные файла, а не ошибка типа аргумента API.
            raise ValueError("Метаданные существующей заметки изменены")  # noqa: TRY004
        return target, text, _clean_title(title)
    return None


def _write_note(folder: Path, key: str, body: str, title: str, tags: list[str]) -> tuple[Path, str, str, bool]:
    """Эксклюзивное создание с суффиксом request_id и разрешением коллизий."""
    existing = _find_existing(folder, key, body)
    if existing is not None:
        return *existing, False
    # JSON-строка/массив — безопасное подмножество YAML, без инъекции frontmatter.
    text = (f"---\nbrain_request_id: {key}\n"
            f"title: {json.dumps(title, ensure_ascii=False)}\n"
            f"tags: {json.dumps(tags, ensure_ascii=False)}\n---\n\n{body}")
    names = {target.name.casefold() for target in folder.iterdir()}
    for counter in range(1000):
        suffix = (str(counter) if counter else "") + key[:_KEY_SUFFIX]
        target = folder / f"{title[:_MAX_NAME - len(suffix)]}{suffix}.md"
        if target.name.casefold() in names:
            continue
        try:
            with target.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
            return target, text, title, True
        except FileExistsError:
            existing = _find_existing(folder, key, body)
            if existing is not None:
                return *existing, False
    raise ValueError("Не удалось подобрать уникальное имя заметки")


def _create_note(content: str, request_id: str, title: str, tags: list[str] | None) -> dict:
    if os.environ.get("BRAIN_WRITE_ENABLED", "false").lower() != "true":
        raise ValueError("Запись выключена: задайте BRAIN_WRITE_ENABLED=true в окружении MCP-сервера")
    if not content.strip() or len(content) > 100_000:
        raise ValueError("Текст должен содержать от 1 до 100000 символов")
    if not request_id or len(request_id) > 200:
        raise ValueError("Нужен request_id длиной до 200 символов")
    if not isinstance(title, str) or (tags is not None and (
        not isinstance(tags, list) or len(tags) > 8 or any(not isinstance(tag, str) for tag in tags)
    )):
        raise ValueError("Нужны строка title и список не более 8 строк tags")
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
    body = content.strip() + "\n"
    title = _clean_title(title or content.strip().splitlines()[0])
    # Сохраняем и исходные теги (включая frontmatter), и добавленные клиентом.
    note_tags = sorted(set(extract_tags(content)) | {tag for raw in tags or [] if (tag := normalize_tag(raw))})
    try:
        folder.mkdir(exist_ok=True)
        if folder.is_symlink() or folder.resolve() != folder:
            raise ValueError("Telegram должен быть обычным каталогом внутри хранилища")
        target, text, title, created = _write_note(folder, key, body, title, note_tags)
    except ValueError:
        raise
    except OSError as exc:
        raise WriteError("file", type(exc).__name__) from exc

    path = f"{vault.name}/Telegram/{target.name}"
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
    return {"path": path, "title": title, "tags": extract_tags(text), "created": created, "git_pushed": True}


def _index_note(db, path, target, content, text, title, digest):
    """Индексировать новый файл; гонка с sync.py не должна ломать запись."""
    tags = extract_tags(text)
    try:
        cursor = db.execute(
            "INSERT INTO notes(path,content_hash,mtime,title,summary,keywords,entities,questions,doc_type,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (path, digest, target.stat().st_mtime, title, content[:1000],
             json.dumps(["Telegram"] + tags, ensure_ascii=False), "[]", "[]", "clippings", datetime.now(timezone.utc).isoformat()),
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
