"""Общее для индексатора и MCP-сервера: пути, подключение к БД, нормализация текста."""
import os
import re
import sqlite3
import stat
from pathlib import Path
from urllib.parse import quote

BRAIN_DIR = Path(__file__).resolve().parent
DB_PATH = BRAIN_DIR / "data" / "brain.db"
SCHEMA = BRAIN_DIR / "schema.sql"


def load_env(path: Path = BRAIN_DIR / ".env", skip: set[str] | None = None):
    """Минимальный разбор .env (KEY=VALUE); переменные окружения имеют приоритет.

    skip — имена переменных, которые не нужно подхватывать (MCP не вызывает LLM
    и не должен держать LLM_API_KEY в своём окружении).
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if skip and key in skip:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


load_env(skip={k.strip() for k in os.environ.get("BRAIN_ENV_SKIP", "").split(",") if k.strip()})


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
        if p.parent == p:  # «/» или «C:\»: иначе индексируется вся ФС
            raise SystemExit(f"VAULT_PATH не может быть корнем файловой системы: {p}")
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


def connect(readonly: bool = False) -> sqlite3.Connection:
    """Подключение к БД. readonly=True — для MCP: без создания файла, DDL и WAL."""
    if readonly:
        if not DB_PATH.exists():
            raise sqlite3.OperationalError("БД ещё не создана — сначала запустите python sync.py")
        # mode=ro не даёт MCP случайно (или через будущий баг) изменить индекс.
        uri = f"file:{quote(DB_PATH.as_posix(), safe='/:')}?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only = ON")
        return db
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")  # чтения (MCP) не блокируются записями sync
    db.executescript(SCHEMA.read_text(encoding="utf-8"))
    _migrate(db)
    return db


def _migrate(db: sqlite3.Connection) -> None:
    """Добавить новые колонки в уже существующие таблицы (без пересборки БД)."""
    columns = {row[1] for row in db.execute("PRAGMA table_info(note_links)")}
    if columns and "shared_terms" not in columns:
        db.execute("ALTER TABLE note_links ADD COLUMN shared_terms TEXT NOT NULL DEFAULT '[]'")
        db.commit()


def _is_link(path: Path) -> bool:
    """Симлинк или junction: может увести чтение за пределы хранилища."""
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None:  # Python 3.12+
        try:
            return bool(is_junction())
        except OSError:
            return True
    if os.name == "nt":  # Python 3.10–3.11: различаем junction по reparse tag
        try:
            info = os.lstat(path)
        except OSError:
            return True
        return getattr(info, "st_reparse_tag", 0) == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)
    return False


def iter_notes():
    """Путь заметки в БД: <имя каталога хранилища>/<путь внутри хранилища>.

    Симлинки и junction не индексируем: иначе заметка-ссылка может прочитать
    файл снаружи хранилища и отправить его в LLM. Повторные пути к одному
    файлу (например, через junction-петлю) схлопываем по resolved-пути.
    """
    for root in VAULTS:
        root = root.resolve()
        seen: set[Path] = set()
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            here = Path(dirpath)
            # служебные каталоги и ссылки исключаем на любой глубине
            dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS and not _is_link(here / d))
            for name in sorted(filenames):
                if not name.endswith(".md") or name in EXCLUDE_FILES:
                    continue
                p = here / name
                if _is_link(p) or not p.is_file():  # FIFO/сокет не читаем: можно зависнуть
                    continue
                real = p.resolve()
                if root not in real.parents or real in seen:
                    continue
                seen.add(real)
                rel = real.relative_to(root)
                yield real, f"{root.name}/{rel.as_posix()}"
