"""Инкрементальная дистилляция хранилища в .brain/brain.db.

  python brain/sync.py            – обработать новые/изменённые, удалить исчезнувшие
  python brain/sync.py --dry-run  – только показать, что будет сделано
  python brain/sync.py --watch    – держать процесс и синхронизировать после правок
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from common import VAULT, connect, iter_notes, stem_text

MAX_CHUNK = 6000           # символов в одном куске
MAX_DISTILL_INPUT = int(os.environ.get("LLM_MAX_INPUT_CHARS", "60000"))  # больше – делаем map-reduce по секциям
PROMPT = Path(__file__).with_name("prompt.md").read_text(encoding="utf-8")

JSON_HINT = (
    "\n\nОтветь ТОЛЬКО одним JSON-объектом без пояснений и markdown: "
    '{"summary": str, "keywords": [str], "entities": [str], "questions": [str], "doc_type": str}'
)

_client = None


def client():
    global _client
    if _client is None:
        from openai import OpenAI

        _client = OpenAI(
            base_url=os.environ.get("LLM_BASE_URL") or None,
            api_key=os.environ.get("LLM_API_KEY") or "none",  # локальным серверам ключ не нужен
            timeout=float(os.environ.get("LLM_TIMEOUT", "120")),
        )
    return _client


def parse_card(raw: str) -> dict:
    """Достаём JSON, даже если модель обернула его в ```json или добавила текст."""
    m = re.search(r"\{.*\}", raw, flags=re.S)
    card = json.loads(m.group(0) if m else raw)
    for key in ("keywords", "entities", "questions"):
        card[key] = [str(x) for x in card.get(key) or []]
    card["summary"] = str(card.get("summary", ""))
    card["doc_type"] = str(card.get("doc_type") or "other")
    return card


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def strip_frontmatter(text: str) -> str:
    return re.sub(r"\A---\n.*?\n---\n", "", text, count=1, flags=re.S)


def chunk_note(text: str) -> list[tuple[str, str]]:
    """Режем по заголовкам, длинные секции – по абзацам."""
    parts, heading, buf = [], "", []
    for line in text.splitlines():
        if re.match(r"#{1,4}\s", line):
            parts.append((heading, "\n".join(buf)))
            heading, buf = line.lstrip("# ").strip(), []
        else:
            buf.append(line)
    parts.append((heading, "\n".join(buf)))

    chunks = []
    for heading, body in parts:
        body = body.strip()
        if not body:
            continue
        cur = ""
        for para in body.split("\n\n"):
            if cur and len(cur) + len(para) > MAX_CHUNK:
                chunks.append((heading, cur))
                cur = ""
            cur = f"{cur}\n\n{para}" if cur else para
        chunks.append((heading, cur))
    return chunks


def call_card(title: str, text: str, hint: str = "") -> dict:
    kwargs = {}
    if os.environ.get("LLM_JSON_MODE", "1") != "0":
        kwargs["response_format"] = {"type": "json_object"}
    resp = client().chat.completions.create(
        model=os.environ["LLM_MODEL"],
        max_tokens=int(os.environ.get("LLM_MAX_TOKENS", "1500")),
        temperature=0.2,
        messages=[
            {"role": "system", "content": PROMPT + JSON_HINT},
            {"role": "user", "content": f"Заголовок: {title}\n{hint}\n\n{text}"},
        ],
        **kwargs,
    )
    return parse_card(resp.choices[0].message.content or "")


def distill(title: str, text: str, chunks: list[tuple[str, str]]) -> dict:
    if len(text) <= MAX_DISTILL_INPUT:
        return call_card(title, text)
    # map: краткие карточки секций, reduce: общая карточка по ним
    partial = []
    for heading, body in chunks:
        card = call_card(title, body, f"Это фрагмент заметки, раздел «{heading}».")
        partial.append(f"## {heading}\n{card['summary']}\nКлючевые слова: {', '.join(card['keywords'])}")
    return call_card(title, "\n\n".join(partial), "Ниже сводки разделов длинной заметки.")


def index_note(db, path: str, abs_path: Path, text: str, h: str):
    title = abs_path.stem
    body = strip_frontmatter(text)
    chunks = chunk_note(body)
    card = distill(title, body, chunks)

    remove_note(db, path)
    cur = db.execute(
        "INSERT INTO notes(path, content_hash, mtime, title, summary, keywords, entities, questions, doc_type, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (path, h, abs_path.stat().st_mtime, title, card["summary"],
         json.dumps(card["keywords"], ensure_ascii=False), json.dumps(card["entities"], ensure_ascii=False),
         json.dumps(card["questions"], ensure_ascii=False), card["doc_type"],
         datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )
    db.execute(
        "INSERT INTO fts_notes(rowid, title, summary, keywords, questions) VALUES (?,?,?,?,?)",
        (cur.lastrowid, stem_text(title), stem_text(card["summary"]),
         stem_text(" ".join(card["keywords"] + card["entities"])), stem_text(" ".join(card["questions"]))),
    )
    for heading, chunk in chunks:
        c = db.execute("INSERT INTO chunks(path, heading, text, chunk_hash) VALUES (?,?,?,?)",
                       (path, heading, chunk, sha(chunk)))
        db.execute("INSERT INTO fts_chunks(rowid, heading, text) VALUES (?,?,?)",
                   (c.lastrowid, stem_text(heading), stem_text(chunk)))


def remove_note(db, path: str):
    row = db.execute("SELECT rowid FROM notes WHERE path=?", (path,)).fetchone()
    if not row:
        return
    db.execute("DELETE FROM fts_notes WHERE rowid=?", (row[0],))
    for (cid,) in db.execute("SELECT id FROM chunks WHERE path=?", (path,)).fetchall():
        db.execute("DELETE FROM fts_chunks WHERE rowid=?", (cid,))
    db.execute("DELETE FROM notes WHERE path=?", (path,))  # chunks – каскадом


def sync(dry_run: bool = False) -> int:
    db = connect()
    known = {r["path"]: r["content_hash"] for r in db.execute("SELECT path, content_hash FROM notes")}
    seen, todo = set(), []
    for abs_path, rel in iter_notes():
        seen.add(rel)
        text = abs_path.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            continue
        h = sha(text)
        if known.get(rel) != h:
            todo.append((rel, abs_path, text, h))
    gone = [p for p in known if p not in seen]

    print(f"к обработке: {len(todo)}, к удалению: {len(gone)}, без изменений: {len(seen) - len(todo)}")
    if dry_run:
        for rel, *_ in todo:
            print("  +", rel)
        for p in gone:
            print("  -", p)
        return 0

    for p in gone:
        remove_note(db, p)
    db.commit()
    for i, (rel, abs_path, text, h) in enumerate(todo, 1):
        try:
            index_note(db, rel, abs_path, text, h)
            db.commit()
            print(f"[{i}/{len(todo)}] {rel}")
        except Exception as e:  # одна заметка не должна ронять весь прогон
            db.rollback()
            print(f"[{i}/{len(todo)}] ОШИБКА {rel}: {e}", file=sys.stderr)
    return 0


def watch(debounce: float = 30.0) -> int:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    dirty = {"t": None}

    class Handler(FileSystemEventHandler):
        def on_any_event(self, event):
            if event.src_path.endswith(".md") and ".obsidian" not in event.src_path and ".git" not in event.src_path:
                dirty["t"] = time.time()

    obs = Observer()
    obs.schedule(Handler(), str(VAULT), recursive=True)
    obs.start()
    sync()
    print("слежу за хранилищем (Ctrl+C для выхода)")
    try:
        while True:
            time.sleep(2)
            if dirty["t"] and time.time() - dirty["t"] >= debounce:
                dirty["t"] = None
                sync()
    except KeyboardInterrupt:
        pass
    finally:
        obs.stop()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--watch", action="store_true")
    args = ap.parse_args()
    sys.exit(watch() if args.watch else sync(args.dry_run))
