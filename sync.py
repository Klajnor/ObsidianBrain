"""Инкрементальная дистилляция хранилищ в data/brain.db.

  python sync.py            – обработать новые/изменённые, удалить исчезнувшие
  python sync.py --dry-run  – только показать, что будет сделано
  python sync.py --watch    – держать процесс и синхронизировать после правок
"""
import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from common import EXCLUDE_DIRS, VAULTS, connect, iter_notes, stem_text
from links import candidate_pairs, classify_pair, save_link
from logging_setup import setup_file_logging
from tags import extract_tags

logger = logging.getLogger(__name__)


MAX_CHUNK = 6000           # символов в одном куске
MAX_DISTILL_INPUT = int(os.environ.get("LLM_MAX_INPUT_CHARS", "60000"))  # больше – делаем map-reduce по секциям
MAX_CHUNKS = max(1, int(os.environ.get("LLM_MAX_CHUNKS", "60")))  # потолок LLM-вызовов на одну заметку
MAX_NOTE_BYTES = max(1024, int(os.environ.get("LLM_MAX_NOTE_BYTES", str(5 * 1024 * 1024))))
PROMPT = Path(__file__).with_name("prompt.md").read_text(encoding="utf-8")

JSON_HINT = (
    "\n\nОтветь ТОЛЬКО одним JSON-объектом без пояснений и markdown: "
    '{"summary": str, "keywords": [str], "entities": [str], "questions": [str], "doc_type": str}'
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


_client = None


def _check_base_url(url: str | None) -> str | None:
    """Не отправлять ключ и заметки открытым текстом по HTTP вне localhost."""
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise SystemExit("LLM_BASE_URL должен начинаться с http:// или https://")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        if os.environ.get("LLM_ALLOW_HTTP", "").lower() not in {"1", "true", "yes"}:
            raise SystemExit(
                "LLM_BASE_URL по HTTP небезопасен: ключ и текст заметок пойдут открытым текстом. "
                "Используйте HTTPS или localhost/127.0.0.1/::1; для осознанного исключения задайте LLM_ALLOW_HTTP=1."
            )
        print(f"ПРЕДУПРЕЖДЕНИЕ: LLM_BASE_URL по HTTP ({parsed.hostname}) — трафик не шифруется",
              file=sys.stderr)
    return url


def client():
    global _client
    if _client is None:
        from openai import OpenAI

        if not os.environ.get("LLM_MODEL"):
            raise SystemExit("Не задан LLM_MODEL (имя модели) – см. .env.example")
        _client = OpenAI(
            base_url=_check_base_url(os.environ.get("LLM_BASE_URL") or None),
            api_key=os.environ.get("LLM_API_KEY") or "none",  # локальным серверам ключ не нужен
            timeout=float(os.environ.get("LLM_TIMEOUT", "120")),
        )
    return _client


CARD_KEYS = {"summary", "keywords", "entities", "questions", "doc_type"}


def _norm_card(card: dict) -> dict:
    for key in ("keywords", "entities", "questions"):
        card[key] = [str(x) for x in card.get(key) or []]
    card["summary"] = str(card.get("summary", ""))
    card["doc_type"] = str(card.get("doc_type") or "other")
    return card


def parse_card(raw: str) -> dict:
    """Достаём JSON-карточку, даже если модель обернула её в ```json или добавила текст."""
    dec = json.JSONDecoder()
    fallback = None
    for i, ch in enumerate(raw):
        if ch != "{":
            continue
        try:
            obj = dec.raw_decode(raw, i)[0]
        except json.JSONDecodeError:
            continue  # `{` в тексте вокруг JSON – пробуем следующую
        if not isinstance(obj, dict):
            continue
        if CARD_KEYS & obj.keys():  # похоже на карточку, а не на случайный объект в тексте
            return _norm_card(obj)
        fallback = fallback or obj
    if fallback is None:
        raise ValueError(f"в ответе модели нет JSON-объекта: {raw[:200]!r}")
    return _norm_card(fallback)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def strip_frontmatter(text: str) -> str:
    return re.sub(r"\A---\n.*?\n---(?:\n|\Z)", "", text, count=1, flags=re.S)


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

    def pieces(body: str):
        # абзац без переносов длиннее лимита (сплошная простыня, base64) режем принудительно
        for para in body.split("\n\n"):
            while len(para) > MAX_CHUNK:
                yield para[:MAX_CHUNK]
                para = para[MAX_CHUNK:]
            yield para

    chunks = []
    for heading, body in parts:
        body = body.strip()
        if not body:
            continue
        cur = ""
        for piece in pieces(body):
            if cur and len(cur) + len(piece) > MAX_CHUNK:
                chunks.append((heading, cur))
                cur = ""
            cur = f"{cur}\n\n{piece}" if cur else piece
        if cur:
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
    if len(chunks) > MAX_CHUNKS:  # защита от случайного/злонамеренного файла-гиганта
        print(f"ПРЕДУПРЕЖДЕНИЕ: заметка {title!r}: {len(chunks)} фрагментов, обрабатываю первые {MAX_CHUNKS}",
              file=sys.stderr)
        chunks = chunks[:MAX_CHUNKS]
    # map: краткие карточки секций, reduce: общая карточка по ним
    partial = []
    for heading, body in chunks:
        card = call_card(title, body, f"Это фрагмент заметки, раздел «{heading}».")
        partial.append(f"## {heading}\n{card['summary']}\nКлючевые слова: {', '.join(card['keywords'])}")
    return call_card(title, "\n\n".join(partial), "Ниже сводки разделов длинной заметки.")


def index_note(db, path: str, abs_path: Path, text: str, h: str, tags: list[str] | None = None):
    title = abs_path.stem
    body = strip_frontmatter(text)
    chunks = chunk_note(body)
    card = distill(title, body, chunks)
    tags = sorted(set(tags if tags is not None else extract_tags(text)))

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
         stem_text(" ".join(card["keywords"] + card["entities"] + tags)), stem_text(" ".join(card["questions"]))),
    )
    for tag in tags:
        db.execute("INSERT OR IGNORE INTO note_tags(path, tag) VALUES (?,?)", (path, tag))
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
    db.execute("DELETE FROM notes WHERE path=?", (path,))  # chunks, теги и связи – каскадом


def sync_note_tags(db, tags_by_path: dict[str, list[str]]) -> int:
    """Обновить теги, в том числе у заметок, проиндексированных старой версией."""
    existing: dict[str, set[str]] = defaultdict(set)
    for row in db.execute("SELECT path, tag FROM note_tags"):
        existing[row["path"]].add(row["tag"])

    updated = 0
    for path, tags in tags_by_path.items():
        tag_set = set(tags)
        if existing.get(path, set()) == tag_set:
            continue
        db.execute("DELETE FROM note_tags WHERE path=?", (path,))
        db.executemany(
            "INSERT INTO note_tags(path, tag) VALUES (?,?)",
            [(path, tag) for tag in sorted(tag_set)],
        )
        # Изменился набор тегов – прежние связи этой заметки больше не актуальны.
        db.execute("DELETE FROM note_links WHERE path_a=? OR path_b=?", (path, path))
        row = db.execute("SELECT rowid, keywords, entities FROM notes WHERE path=?", (path,)).fetchone()
        if row:
            words = json.loads(row["keywords"]) + json.loads(row["entities"]) + sorted(tag_set)
            db.execute(
                "UPDATE fts_notes SET keywords=? WHERE rowid=?",
                (stem_text(" ".join(words)), row["rowid"]),
            )
        updated += 1
    return updated


def _link_candidates(db, changed: set[str]):
    """Кандидаты связей по общим тегам/терминам; [] если этап связей отключён."""
    if os.environ.get("BRAIN_LINKS_ENABLED", "1").lower() not in {"1", "true", "yes"}:
        return []
    if _env_int("BRAIN_LINK_MAX_PAIRS", 100) <= 0:
        return []
    return candidate_pairs(
        db,
        changed,
        min_score=_env_float("BRAIN_LINK_MIN_SCORE", 0.65),
        max_per_note=_env_int("BRAIN_LINK_MAX_PER_NOTE", 12),
        max_tag_fanout=_env_int("BRAIN_LINK_MAX_TAG_FANOUT", 40),
        min_shared_terms=_env_int("BRAIN_LINK_MIN_SHARED_TERMS", 2),
        max_term_fanout=_env_int("BRAIN_LINK_MAX_TERM_FANOUT", 30),
    )


def build_links(db, changed: set[str]) -> int:
    """Оценить через LLM пары заметок с общими тегами. Возвращает число ошибок."""
    candidates = _link_candidates(db, changed)
    if not candidates:
        return 0
    todo = candidates[:max(1, _env_int("BRAIN_LINK_MAX_PAIRS", 100))]
    print(f"связи: кандидатов {len(candidates)}, сравню {len(todo)}")
    if len(candidates) > len(todo):
        print(f"связи: ещё {len(candidates) - len(todo)} останутся на следующий прогон")

    failed = 0
    max_tokens = max(100, _env_int("LLM_LINK_MAX_TOKENS", 600))
    for i, candidate in enumerate(todo, 1):
        rows = {
            row["path"]: row
            for row in db.execute(
                "SELECT path, title, summary FROM notes WHERE path IN (?,?)",
                (candidate.path_a, candidate.path_b),
            )
        }
        if candidate.path_a not in rows or candidate.path_b not in rows:
            continue
        tags: dict[str, list[str]] = defaultdict(list)
        for row in db.execute(
            "SELECT path, tag FROM note_tags WHERE path IN (?,?) ORDER BY tag",
            (candidate.path_a, candidate.path_b),
        ):
            tags[row["path"]].append(row["tag"])
        card_a = {
            "title": rows[candidate.path_a]["title"],
            "summary": rows[candidate.path_a]["summary"],
            "tags": tags[candidate.path_a],
        }
        card_b = {
            "title": rows[candidate.path_b]["title"],
            "summary": rows[candidate.path_b]["summary"],
            "tags": tags[candidate.path_b],
        }
        try:
            result = classify_pair(
                client(),
                os.environ["LLM_MODEL"],
                card_a,
                card_b,
                candidate.shared_tags,
                shared_terms=candidate.shared_terms,
                max_tokens=max_tokens,
            )
            save_link(db, candidate, result, model=os.environ["LLM_MODEL"])
            db.commit()
            print(f"[связи {i}/{len(todo)}] {candidate.path_a!r} <-> {candidate.path_b!r}: {result['score']:.2f}")
        except Exception as exc:  # одна пара не должна ронять весь прогон
            db.rollback()
            failed += 1
            print(
                f"[связи {i}/{len(todo)}] ОШИБКА {candidate.path_a!r} <-> {candidate.path_b!r}: {exc!r}",
                file=sys.stderr,
            )
            logger.error(
                "[связи %s/%s] ОШИБКА %r <-> %r: %r",
                i, len(todo), candidate.path_a, candidate.path_b, exc,
            )
    if failed:
        print(f"ОШИБОК СВЯЗЕЙ: {failed}", file=sys.stderr)
        logger.error("ОШИБОК СВЯЗЕЙ: %s", failed)
    return failed


def sync(dry_run: bool = False) -> int:
    db = connect()
    known = {r["path"]: r["content_hash"] for r in db.execute("SELECT path, content_hash FROM notes")}
    seen, todo = set(), []
    tags_by_path: dict[str, list[str]] = {}
    total = skipped_size = skipped_error = skipped_empty = 0
    for abs_path, rel in iter_notes():
        total += 1
        try:
            if abs_path.stat().st_size > MAX_NOTE_BYTES:
                skipped_size += 1
                print(f"ПРОПУСК {rel!r}: файл больше {MAX_NOTE_BYTES} байт", file=sys.stderr)
                logger.warning("ПРОПУСК %r: файл больше %s байт", rel, MAX_NOTE_BYTES)
                if rel in known:
                    seen.add(rel)  # прежнюю запись не удаляем, пока файл не уменьшится
                continue
            text = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:  # недоступный файл не должен ронять прогон
            skipped_error += 1
            if rel in known:
                seen.add(rel)  # временно не читается – прежнюю запись из БД не удаляем
            print(f"ПРОПУСК {rel!r}: {e!r}", file=sys.stderr)
            logger.warning("ПРОПУСК %r: %r", rel, e)
            continue
        if not text.strip():
            skipped_empty += 1
            continue  # пустая заметка не индексируется; прежняя запись уйдёт в «исчезнувшие»
        seen.add(rel)
        tags_by_path[rel] = extract_tags(text)
        h = sha(text)
        if known.get(rel) != h:
            todo.append((rel, abs_path, text, h))
    gone = [p for p in known if p not in seen]

    skipped = skipped_size + skipped_error + skipped_empty
    print(
        f"всего найдено файлов: {total}; к обработке: {len(todo)}, "
        f"к удалению: {len(gone)}, без изменений: {len(seen) - len(todo)}, пропущено: {skipped}"
    )
    if dry_run:
        if skipped:
            print(
                f"  пропуски: больше лимита {skipped_size}, ошибки чтения {skipped_error}, "
                f"пустых {skipped_empty}"
            )
        for rel, *_ in todo:
            print(f"  + {rel!r}")
        for p in gone:
            print(f"  - {p!r}")
        links_candidates = _link_candidates(db, {rel for rel, *_ in todo})
        if links_candidates:
            # Оценка по текущему индексу: теги только что изменённых заметок
            # попадут в кандидатов после реальной индексации.
            print(f"связи: кандидатов {len(links_candidates)}")
        return 0

    for p in gone:
        remove_note(db, p)
    db.commit()
    failed = 0
    changed_ok: set[str] = set()
    failed_paths: set[str] = set()
    for i, (rel, abs_path, text, h) in enumerate(todo, 1):
        try:
            index_note(db, rel, abs_path, text, h, tags_by_path.get(rel))
            db.commit()
            changed_ok.add(rel)
            print(f"[{i}/{len(todo)}] {rel!r}")
        except Exception as e:  # одна заметка не должна ронять весь прогон
            db.rollback()
            failed += 1
            failed_paths.add(rel)
            print(f"[{i}/{len(todo)}] ОШИБКА {rel!r}: {e!r}", file=sys.stderr)
            logger.error("[%s/%s] ОШИБКА %r: %r", i, len(todo), rel, e)

    # Теги обновляем и у неизменённых заметок: так старый индекс получает теги
    # без полного пересчёта LLM-карточек.
    updated_tags = sync_note_tags(
        db, {path: tags for path, tags in tags_by_path.items() if path not in failed_paths}
    )
    db.commit()
    if updated_tags:
        print(f"теги обновлены: {updated_tags}")

    failed += build_links(db, changed_ok)
    if failed:
        print(f"ОШИБОК: {failed}", file=sys.stderr)
        logger.error("ОШИБОК: %s", failed)
    return 1 if failed else 0


def watch(debounce: float = 30.0) -> int:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    dirty = {"t": None}

    class Handler(FileSystemEventHandler):
        def on_any_event(self, event):
            src = (event.src_path or "").replace("\\", "/")
            if src.endswith(".md") and not any(f"/{d}/" in src for d in EXCLUDE_DIRS):
                dirty["t"] = time.time()

    obs = Observer()
    for v in VAULTS:
        obs.schedule(Handler(), str(v), recursive=True)
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
    setup_file_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--watch", action="store_true")
    args = ap.parse_args()
    sys.exit(watch() if args.watch else sync(args.dry_run))
