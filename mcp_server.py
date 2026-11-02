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
from capture import create_note as capture_note

os.environ.pop("LLM_API_KEY", None)

mcp = MCPServer("brain")


def _card(r) -> dict:
    return {"path": r["path"], "title": r["title"], "type": r["doc_type"], "summary": r["summary"],
            "keywords": json.loads(r["keywords"])}


@mcp.tool()
def search_notes(query: str, k: int = 8) -> list[dict]:
    """Поиск заметок по ключевым словам/вопросу. Возвращает карточки (путь, описание, ключевые слова)
    и до 2 лучших фрагментов. Полный текст – через get_note, фрагмент – через get_chunk."""
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
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else "ошибка: файл не найден"


@mcp.tool()
def list_notes(doc_type: str = "", limit: int = 50) -> list[dict]:
    """Список заметок (опционально по типу: howto, reference, daily, clippings, troubleshooting)."""
    limit = max(1, min(limit, 500))
    sql, args = "SELECT * FROM notes", []
    if doc_type:
        sql, args = sql + " WHERE doc_type=?", [doc_type]
    return [_card(r) for r in connect(readonly=True).execute(sql + " ORDER BY updated_at DESC LIMIT ?", args + [limit])]


@mcp.tool()
def create_note(content: str, request_id: str) -> dict:
    """Создать новую Telegram-заметку (нужен BRAIN_WRITE_ENABLED=true).

    Не изменяет существующие файлы. Повтор с тем же request_id не создаёт дубликат.
    Авторизацию владельца обязан проверить вызывающий клиент.
    """
    return capture_note(content, request_id)


if __name__ == "__main__":
    mcp.run()
