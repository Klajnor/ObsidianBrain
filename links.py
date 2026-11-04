"""Связность заметок: кандидаты по общим тегам и LLM-оценка пар саммари."""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

LINK_PROMPT = Path(__file__).with_name("link_prompt.md").read_text(encoding="utf-8")
_LINK_KEYS = {"score", "common", "reason"}
MAX_FALLBACK_PAIRS = 100_000  # защита памяти, если в хранилище очень много заметок без тегов


@dataclass(frozen=True)
class Candidate:
    path_a: str
    path_b: str
    shared_tags: tuple[str, ...]
    weight: float
    shared_terms: tuple[str, ...] = ()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _norm_term(value) -> str:
    """Нормализовать keyword/entity для сравнения без стемминга и без потерь."""
    term = unicodedata.normalize("NFC", str(value)).strip().strip("\"'").casefold()
    term = re.sub(r"\s+", " ", term).strip(" .,:;!?()[]{}<>")
    if len(term) < 2:
        return ""
    return term[:200]


def _note_terms(row) -> set[str]:
    terms: set[str] = set()
    for field in ("keywords", "entities"):
        try:
            values = json.loads(row[field] or "[]")
        except (TypeError, ValueError):
            continue
        if not isinstance(values, list):
            continue
        for value in values:
            term = _norm_term(value)
            if term:
                terms.add(term)
            if len(terms) >= 100:  # защита от аномально большой карточки
                return terms
    return terms


def _join_labels(values, limit: int = 50) -> str:
    if isinstance(values, str):
        values = [values]
    return ", ".join(str(value) for value in list(values)[:limit])


def candidate_pairs(
    db,
    changed: set[str] | None = None,
    *,
    min_score: float = 0.65,
    max_per_note: int = 12,
    max_tag_fanout: int = 40,
    min_shared_terms: int = 2,
    max_term_fanout: int = 30,
    max_attempts: int = 3,
) -> list[Candidate]:
    """Пары-кандидаты: сначала общие теги, затем fallback по keywords/entities.

    Теги остаются главным критерием: fallback добавляет только пары, в которых
    ни у одной заметки нет теговой пары-кандидата. Массовые теги и термины
    пропускаются, иначе один общий термин порождает тысячи пар. Пары, набравшие
    max_attempts ошибок ответа модели, не выбираются (0 — без ограничения).
    """
    if max_per_note <= 0 or max_tag_fanout < 2:
        return []
    min_score = max(0.0, min(float(min_score), 1.0))
    changed = changed or set()

    rows = db.execute(
        "SELECT path, title, summary, keywords, entities FROM notes"
    ).fetchall()
    meta: dict[str, dict] = {}
    for row in rows:
        meta[row["path"]] = {
            "title": row["title"],
            "summary": row["summary"],
            "terms": _note_terms(row),
        }
    if len(meta) < 2:
        return []

    by_tag: dict[str, list[str]] = defaultdict(list)
    for row in db.execute("SELECT path, tag FROM note_tags ORDER BY tag, path"):
        by_tag[row["tag"]].append(row["path"])

    analyzed = {
        (row["path_a"], row["path_b"])
        for row in db.execute("SELECT path_a, path_b FROM note_links")
    }
    failed: set[tuple[str, str]] = set()
    if max_attempts > 0:
        try:
            failed = {
                (row["path_a"], row["path_b"])
                for row in db.execute(
                    "SELECT path_a, path_b FROM link_failures WHERE attempts >= ?",
                    (max_attempts,),
                )
            }
        except sqlite3.OperationalError:  # БД старой версии без таблицы ошибок
            failed = set()
    good = Counter()
    for row in db.execute(
        "SELECT path_a, path_b FROM note_links WHERE score >= ?", (min_score,)
    ):
        good[row["path_a"]] += 1
        good[row["path_b"]] += 1

    total = len(meta)
    pair_info: dict[tuple[str, str], dict] = {}

    def add_pair(path_a: str, path_b: str, *, tag: str = "", term: str = "", weight: float):
        info = pair_info.setdefault(
            (path_a, path_b), {"tags": [], "terms": [], "weight": 0.0}
        )
        if tag:
            info["tags"].append(tag)
        if term:
            info["terms"].append(term)
        info["weight"] += weight

    tag_candidate_notes: set[str] = set()
    for tag, paths in by_tag.items():
        paths = [path for path in sorted(set(paths)) if path in meta]
        if len(paths) < 2 or len(paths) > max_tag_fanout:
            continue
        weight = math.log(1 + total / len(paths))
        for i, path_a in enumerate(paths):
            for path_b in paths[i + 1:]:
                add_pair(path_a, path_b, tag=tag, weight=weight)
                if meta[path_a]["summary"].strip() and meta[path_b]["summary"].strip():
                    tag_candidate_notes.update((path_a, path_b))

    # Fallback: нужен хотя бы один общий keyword/entity, а лучше несколько.
    if min_shared_terms > 0 and max_term_fanout >= 2:
        by_term: dict[str, list[str]] = defaultdict(list)
        for path, info in meta.items():
            for term in info["terms"]:
                by_term[term].append(path)
        fallback_pairs = 0
        for term, paths in by_term.items():
            paths = sorted(set(paths))
            if len(paths) < 2 or len(paths) > max_term_fanout:
                continue
            weight = math.log(1 + total / len(paths))
            for i, path_a in enumerate(paths):
                if path_a in tag_candidate_notes:
                    continue
                for path_b in paths[i + 1:]:
                    if path_b in tag_candidate_notes:
                        continue
                    add_pair(path_a, path_b, term=term, weight=weight)
                    fallback_pairs += 1
                    if fallback_pairs >= MAX_FALLBACK_PAIRS:
                        break
                if fallback_pairs >= MAX_FALLBACK_PAIRS:
                    break
            if fallback_pairs >= MAX_FALLBACK_PAIRS:
                break

    def priority(item: tuple[tuple[str, str], dict]):
        (path_a, path_b), info = item
        # Сначала пары с изменённой заметкой, затем теговые, затем fallback.
        return (
            0 if path_a in changed or path_b in changed else 1,
            0 if info["tags"] else 1,
            -info["weight"],
            path_a,
            path_b,
        )

    selected: list[Candidate] = []
    for (path_a, path_b), info in sorted(pair_info.items(), key=priority):
        if (path_a, path_b) in analyzed or (path_a, path_b) in failed:
            continue
        if not info["tags"] and len(set(info["terms"])) < min_shared_terms:
            continue
        if good[path_a] >= max_per_note or good[path_b] >= max_per_note:
            continue
        if not meta[path_a]["summary"].strip() or not meta[path_b]["summary"].strip():
            continue
        selected.append(Candidate(
            path_a=path_a,
            path_b=path_b,
            shared_tags=tuple(sorted(set(info["tags"]))),
            weight=info["weight"],
            shared_terms=tuple(sorted(set(info["terms"]))),
        ))
        good[path_a] += 1
        good[path_b] += 1
    return selected


def _norm_link(obj: dict) -> dict:
    raw_score = obj.get("score", 0)
    if isinstance(raw_score, str):
        raw_score = raw_score.replace(",", ".").strip()
    try:
        score = float(raw_score)
    except (TypeError, ValueError) as exc:
        raise ValueError("в ответе модели нет числового score") from exc
    if score > 1:  # некоторые модели отвечают в шкале 10 или 100
        score = score / 100 if score > 10 else score / 10
    score = max(0.0, min(score, 1.0))

    raw_common = obj.get("common") or []
    if isinstance(raw_common, str):
        raw_common = [raw_common]
    common = [str(item).strip() for item in raw_common if str(item).strip()][:5]
    reason = str(obj.get("reason") or "").strip()[:1000]
    return {"score": score, "common": common, "reason": reason}


def parse_link(raw: str) -> dict:
    """Достаём JSON-оценку пары, даже если модель обернула её в текст."""
    decoder = json.JSONDecoder()
    for i, char in enumerate(raw):
        if char != "{":
            continue
        try:
            obj = decoder.raw_decode(raw, i)[0]
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and _LINK_KEYS & obj.keys():
            return _norm_link(obj)
    raise ValueError(f"в ответе модели нет JSON-объекта: {raw[:200]!r}")


def _answer_text(response) -> tuple[str, str, str | None]:
    """Текст ответа, reasoning-текст (если провайдер его прислал) и finish_reason."""
    choice = response.choices[0]
    message = choice.message
    content = str(getattr(message, "content", None) or "")
    reasoning = str(
        getattr(message, "reasoning_content", None)
        or getattr(message, "reasoning", None)
        or ""
    )
    return content, reasoning, getattr(choice, "finish_reason", None)


def _parse_answer(response, max_tokens: int) -> dict:
    """Разобрать ответ; пустой/обрезанный ответ – понятная ошибка, а не «нет JSON»."""
    content, reasoning, finish = _answer_text(response)
    for raw in (content, reasoning):
        if not raw.strip():
            continue
        try:
            return parse_link(raw)
        except ValueError:
            continue
    if finish == "length":
        raise ValueError(
            f"ответ модели обрезан по лимиту токенов (finish_reason=length, max_tokens={max_tokens})"
        )
    if not content.strip() and not reasoning.strip():
        raise ValueError(
            f"модель вернула пустой ответ (finish_reason={finish!r}, max_tokens={max_tokens})"
        )
    raise ValueError(f"в ответе модели нет JSON-объекта: {content[:200]!r}")


def classify_pair(
    client,
    model: str,
    card_a: dict,
    card_b: dict,
    shared_tags: tuple[str, ...] | list[str],
    *,
    shared_terms: tuple[str, ...] | list[str] = (),
    max_tokens: int = 1200,
) -> dict:
    """Сравнить две карточки заметок; при пустом/битом ответе – повтор с запасом."""
    content = (
        f"Общие теги: {_join_labels(shared_tags) or '—'}\n"
        f"Общие ключевые слова/сущности: {_join_labels(shared_terms) or '—'}\n\n"
        f"Заметка A\nЗаголовок: {str(card_a.get('title', ''))[:300]}\n"
        f"Теги: {_join_labels(card_a.get('tags') or []) or '—'}\n"
        f"Саммари: {str(card_a.get('summary', ''))[:4000]}\n\n"
        f"Заметка B\nЗаголовок: {str(card_b.get('title', ''))[:300]}\n"
        f"Теги: {_join_labels(card_b.get('tags') or []) or '—'}\n"
        f"Саммари: {str(card_b.get('summary', ''))[:4000]}"
    )
    kwargs = {}
    if os.environ.get("LLM_JSON_MODE", "1") != "0":
        kwargs["response_format"] = {"type": "json_object"}
    budget = max(100, int(max_tokens))
    retries = max(0, min(_env_int("LLM_LINK_RETRIES", 1), 5))
    cap = max(budget, _env_int("LLM_LINK_MAX_TOKENS_CAP", 4000))
    for attempt in range(retries + 1):
        response = client.chat.completions.create(
            model=model,
            max_tokens=budget,
            temperature=0.0,
            messages=[
                {"role": "system", "content": LINK_PROMPT},
                {"role": "user", "content": content},
            ],
            **kwargs,
        )
        try:
            return _parse_answer(response, budget)
        except ValueError as exc:
            if attempt >= retries:
                raise ValueError(f"{exc} (попыток: {attempt + 1})") from exc
            # Обрезанный ответ лечится запасом токенов; пустой – повтором запроса.
            budget = min(max(budget * 2, 1000), cap)
    raise AssertionError("недостижимо: цикл всегда возвращает результат или бросает ошибку")


def save_link(db, candidate: Candidate, result: dict, model: str = "") -> None:
    db.execute(
        "INSERT INTO note_links(path_a,path_b,score,shared_tags,shared_terms,common_themes,reason,model,updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(path_a,path_b) DO UPDATE SET score=excluded.score,"
        " shared_tags=excluded.shared_tags, shared_terms=excluded.shared_terms,"
        " common_themes=excluded.common_themes, reason=excluded.reason,"
        " model=excluded.model, updated_at=excluded.updated_at",
        (
            candidate.path_a,
            candidate.path_b,
            float(result["score"]),
            json.dumps(list(candidate.shared_tags), ensure_ascii=False),
            json.dumps(list(candidate.shared_terms), ensure_ascii=False),
            json.dumps(result.get("common") or [], ensure_ascii=False),
            str(result.get("reason") or ""),
            model,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )
    # Успешная оценка снимает счётчик прошлых ошибок этой пары.
    db.execute(
        "DELETE FROM link_failures WHERE path_a=? AND path_b=?",
        (candidate.path_a, candidate.path_b),
    )


def record_link_failure(db, path_a: str, path_b: str, error: str) -> int:
    """Запомнить неудачную попытку пары; вернуть общее число попыток."""
    db.execute(
        "INSERT INTO link_failures(path_a,path_b,attempts,last_error,updated_at)"
        " VALUES (?,?,1,?,?)"
        " ON CONFLICT(path_a,path_b) DO UPDATE SET attempts=attempts+1,"
        " last_error=excluded.last_error, updated_at=excluded.updated_at",
        (
            path_a,
            path_b,
            str(error)[:500],
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )
    row = db.execute(
        "SELECT attempts FROM link_failures WHERE path_a=? AND path_b=?",
        (path_a, path_b),
    ).fetchone()
    return int(row[0]) if row else 1


def reset_links(db) -> int:
    """Удалить кэш оценок и счётчики ошибок: следующий прогон пересчитает пары."""
    removed = max(0, db.execute("DELETE FROM note_links").rowcount)
    db.execute("DELETE FROM link_failures")
    return removed


def _json_list(value: str) -> list[str]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def related(db, path: str, limit: int = 10, min_score: float = 0.65,
            include_summary: bool = True) -> list[dict]:
    """Связанные заметки по сохранённым LLM-оценкам."""
    limit = max(1, min(int(limit), 50))
    if min_score is None:
        min_score = 0.65
    min_score = max(0.0, min(float(min_score), 1.0))
    rows = db.execute(
        "SELECT n.path, n.title, n.summary, l.* FROM note_links l JOIN notes n"
        " ON n.path = CASE WHEN l.path_a = ? THEN l.path_b ELSE l.path_a END"
        " WHERE (l.path_a = ? OR l.path_b = ?) AND l.score >= ?"
        " ORDER BY l.score DESC, n.title COLLATE NOCASE LIMIT ?",
        (path, path, path, min_score, limit),
    ).fetchall()
    out = []
    for row in rows:
        shared_terms = _json_list(row["shared_terms"]) if "shared_terms" in row.keys() else []
        item = {
            "path": row["path"],
            "title": row["title"],
            "score": round(float(row["score"]), 3),
            "shared_tags": _json_list(row["shared_tags"]),
            "shared_terms": shared_terms,
            "common": _json_list(row["common_themes"]),
            "reason": row["reason"],
        }
        if include_summary:
            item["summary"] = row["summary"]
        out.append(item)
    return out
