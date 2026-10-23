"""Общее для индексатора и MCP-сервера: пути, подключение к БД, нормализация текста."""
import os
import re
import sqlite3
from pathlib import Path

BRAIN_DIR = Path(__file__).resolve().parent
DB_PATH = BRAIN_DIR / "data" / "brain.db"
SCHEMA = BRAIN_DIR / "schema.sql"


def load_env(path: Path = BRAIN_DIR / ".env"):
    """Минимальный разбор .env (KEY=VALUE); переменные окружения имеют приоритет."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


load_env()


def _vaults() -> list[Path]:
    """VAULT_PATH: несколько хранилищ через «;»; относительные пути – от каталога с кодом."""
    out: list[Path] = []
    for part in os.environ.get("VAULT_PATH", "").split(";"):
        part = part.strip()
        if not part:
            continue
        p = Path(part)
        if not p.is_absolute():
            p = BRAIN_DIR / p
        p = p.resolve()
        if not p.is_dir():
            raise SystemExit(f"VAULT_PATH не существует: {p}")
        if p in out:  # один и тот же каталог дважды
            continue
        if any(v.name == p.name for v in out):
            raise SystemExit(f"Два разных хранилища с именем «{p.name}» – пути заметок в БД столкнутся")
        out.append(p)
    if not out:
        raise SystemExit("Не задан VAULT_PATH (пути к хранилищам Obsidian, можно несколько через «;») – см. .env.example")
    return out


VAULTS = _vaults()

EXCLUDE_DIRS = {".obsidian", ".git", ".trash", "_resources", "Блок схемы для документации"}
EXCLUDE_FILES = {"CLAUDE.md"}

try:
    import Stemmer  # PyStemmer

    _ru, _en = Stemmer.Stemmer("russian"), Stemmer.Stemmer("english")
except ImportError:  # без пакета – грубое усечение окончаний
    _ru = _en = None

_TOKEN = re.compile(r"\w+", re.UNICODE)
_CYR = re.compile(r"[а-яё]", re.I)


def _stem(word: str) -> str:
    word = word.lower().replace("ё", "е")
    if _ru:
        return (_ru if _CYR.search(word) else _en).stemWord(word)
    return word[:6] if len(word) > 6 else word


def stem_text(text: str) -> str:
    return " ".join(_stem(w) for w in _TOKEN.findall(text))


def fts_query(query: str) -> str:
    """Запрос пользователя -> безопасное FTS5-выражение (OR по основам, префиксный поиск)."""
    terms = [_stem(w) for w in _TOKEN.findall(query) if len(w) > 1]
    return " OR ".join(f'"{t}"*' for t in terms)


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(SCHEMA.read_text(encoding="utf-8"))
    return db


def iter_notes():
    """Путь заметки в БД: <имя каталога хранилища>/<путь внутри хранилища>."""
    for root in VAULTS:
        for p in sorted(root.rglob("*.md")):
            rel = p.relative_to(root)
            if rel.parts[0] in EXCLUDE_DIRS or p.name in EXCLUDE_FILES:
                continue
            yield p, f"{root.name}/{rel.as_posix()}"
