"""MCP-сервер «второго мозга»: поиск по карточкам и выдача текста заметок."""
import json
import os

# MCP не вызывает LLM: не подхватываем ключ из .env и убираем его из окружения
# (он мог прийти от родительского процесса). Сделано до импорта common.
os.environ["BRAIN_ENV_SKIP"] = ",".join(
    filter(None, ["LLM_API_KEY", os.environ.get("BRAIN_ENV_SKIP", "")])
)

from mcp.server.mcpserver import MCPServer

from common import EXCLUDE_DIRS, EXCLUDE_FILES, VAULTS, connect, fts_query

os.environ.pop("LLM_API_KEY", None)

mcp = MCPServer("brain")

MAX_QUERY_CHARS = 500       # защита FTS от гигантских запросов
MAX_K = 50                  # потолок выдачи search_notes
MAX_NOTE_BYTES = 1_000_000  # больше — отдаём через get_chunk, не забиваем контекст


def _card(r) -> dict:
    return {"path": r["path"], "title": r["title"], "type": r["doc_type"], "summary": r["summary"],
            "keywords": json.loads(r["keywords"])}


@mcp.tool()
def search_notes(query: str, k: int = 8) -> list[dict]:
    """Поиск заметок по ключевым словам/вопросу. Возвращает карточки (путь, описание, ключевые слова)
    и до 2 лучших фрагментов. Полный текст – через get_note, фрагмент – через get_chunk."""
    query = query[:MAX_QUERY_CHARS]
    k = max(1, min(int(k), MAX_K))
    q = fts_query(query)
    if not q:
        return []
    db = connect(readonly=True)
    # bm25 отрицательный: меньше = лучше. Карточка весит больше тела заметки.
    scores: dict[str, float] = {}
    for r in db.execute(
        "SELECT n.path, bm25(fts_notes, 8, 3, 6, 4) AS s FROM fts_notes JOIN notes n ON n.rowid = fts_notes.rowid"
        " WHERE fts_notes MATCH ? ORDER BY s LIMIT 30", (q,)):
        scores[r["path"]] = scores.get(r["path"], 0) + r["s"] * 2
    snippets: dict[str, list[dict]] = {}
    for r in db.execute(
        "SELECT c.path, c.id, c.heading, bm25(fts_chunks, 3, 1) AS s FROM fts_chunks JOIN chunks c ON c.id = fts_chunks.rowid"
        " WHERE fts_chunks MATCH ? ORDER BY s LIMIT 40", (q,)):
        scores[r["path"]] = scores.get(r["path"], 0) + r["s"]
        if len(snippets.setdefault(r["path"], [])) < 2:
            snippets[r["path"]].append({"chunk_id": r["id"], "heading": r["heading"]})
    out = []
    for path in sorted(scores, key=scores.get)[:k]:
        row = db.execute("SELECT * FROM notes WHERE path=?", (path,)).fetchone()
        if row:
            out.append({**_card(row), "chunks": snippets.get(path, [])})
    return out


@mcp.tool()
def get_chunk(chunk_id: int) -> dict:
    """Текст одного фрагмента заметки (id из search_notes)."""
    r = connect(readonly=True).execute("SELECT path, heading, text FROM chunks WHERE id=?", (chunk_id,)).fetchone()
    return dict(r) if r else {"error": "не найдено"}


@mcp.tool()
def get_note(path: str) -> str:
    """Полный текст заметки по пути из search_notes (путь начинается с имени хранилища)."""
    path = path.replace("\\", "/")
    vault = next((v for v in VAULTS if path.startswith(v.name + "/")), None)
    if vault is None:
        return "ошибка: путь должен начинаться с имени хранилища: " + "; ".join(v.name for v in VAULTS)
    p = (vault / path[len(vault.name) + 1:]).resolve()
    if (vault not in p.parents or p.suffix != ".md"
            or any(part in EXCLUDE_DIRS for part in p.parts) or p.name in EXCLUDE_FILES):
        return "ошибка: недопустимый путь"
    try:
        if not p.is_file():  # в т.ч. FIFO/каталог: не блокируем чтение
            return "ошибка: не файл"
        if p.stat().st_size > MAX_NOTE_BYTES:
            return f"ошибка: заметка больше {MAX_NOTE_BYTES} байт — используйте get_chunk"
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"ошибка: не удалось прочитать файл: {e}"


@mcp.tool()
def list_notes(doc_type: str = "", limit: int = 50) -> list[dict]:
    """Список заметок (опционально по типу: howto, reference, daily, clippings, troubleshooting)."""
    limit = max(1, min(limit, 500))
    sql, args = "SELECT * FROM notes", []
    if doc_type:
        sql, args = sql + " WHERE doc_type=?", [doc_type]
    return [_card(r) for r in connect(readonly=True).execute(sql + " ORDER BY updated_at DESC LIMIT ?", args + [limit])]


if os.environ.get("BRAIN_WRITE_ENABLED", "false").lower() == "true":
    from capture import WriteError, create_note as capture_note

    @mcp.tool()
    def create_note(content: str, request_id: str) -> dict:
        """Создать новую Telegram-заметку и отправить её в Git (BRAIN_WRITE_ENABLED=true).

        Не изменяет существующие файлы. Повтор с тем же request_id не создаёт дубликат.
        Авторизацию владельца обязан проверить вызывающий клиент.
        """
        try:
            return capture_note(content, request_id)
        except WriteError as exc:
            # Код стадии уже безопасен: без путей и текста заметки.
            return {"git_pushed": False, "error": str(exc)}
        except ValueError as exc:
            # Ошибка валидации/конфликта request_id; текст фиксированный.
            return {"git_pushed": False, "error": f"stage=validate: {type(exc).__name__}"}
        except RuntimeError as exc:
            message = str(exc)
            if message.startswith((
                "Ошибка Git (", "Git недоступен", "Хранилище должно быть",
                "Для ветки", "Есть посторонние", "Созданный файл",
            )):
                return {"git_pushed": False, "error": message}
            return {"git_pushed": False, "error": f"stage=internal: {type(exc).__name__}"}
        except Exception as exc:  # noqa: BLE001 - вернуть клиенту безопасную стадию
            return {"git_pushed": False, "error": f"stage=internal: {type(exc).__name__}"}


if __name__ == "__main__":
    mcp.run()
