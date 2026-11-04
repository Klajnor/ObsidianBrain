CREATE TABLE IF NOT EXISTS notes (
    path         TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    mtime        REAL NOT NULL,
    title        TEXT NOT NULL,
    summary      TEXT NOT NULL,
    keywords     TEXT NOT NULL,   -- JSON list
    entities     TEXT NOT NULL,   -- JSON list
    questions    TEXT NOT NULL,   -- JSON list
    doc_type     TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    id         INTEGER PRIMARY KEY,
    path       TEXT NOT NULL REFERENCES notes(path) ON DELETE CASCADE,
    heading    TEXT NOT NULL,
    text       TEXT NOT NULL,
    chunk_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);

-- Теги Obsidian (frontmatter и инлайновые #теги), нормализованные tags.py.
CREATE TABLE IF NOT EXISTS note_tags (
    path TEXT NOT NULL REFERENCES notes(path) ON DELETE CASCADE,
    tag  TEXT NOT NULL,
    PRIMARY KEY (path, tag)
);
CREATE INDEX IF NOT EXISTS note_tags_tag ON note_tags(tag);

-- Оценённые ИИ пары заметок; path_a < path_b, пара неупорядоченная.
CREATE TABLE IF NOT EXISTS note_links (
    path_a        TEXT NOT NULL REFERENCES notes(path) ON DELETE CASCADE,
    path_b        TEXT NOT NULL REFERENCES notes(path) ON DELETE CASCADE,
    score         REAL NOT NULL,
    shared_tags   TEXT NOT NULL,   -- JSON list
    shared_terms  TEXT NOT NULL DEFAULT '[]',  -- JSON list: keywords/entities fallback
    common_themes TEXT NOT NULL,   -- JSON list
    reason        TEXT NOT NULL,
    model         TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (path_a, path_b),
    CHECK (path_a < path_b)
);
CREATE INDEX IF NOT EXISTS note_links_b ON note_links(path_b);
CREATE INDEX IF NOT EXISTS note_links_score ON note_links(score);

-- Неудачные LLM-сравнения пар: чтобы «ядовитая» пара (пустой/обрезанный ответ)
-- не занимала очередь каждый прогон, после N ошибок она откладывается.
CREATE TABLE IF NOT EXISTS link_failures (
    path_a     TEXT NOT NULL REFERENCES notes(path) ON DELETE CASCADE,
    path_b     TEXT NOT NULL REFERENCES notes(path) ON DELETE CASCADE,
    attempts   INTEGER NOT NULL DEFAULT 1,
    last_error TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (path_a, path_b),
    CHECK (path_a < path_b)
);

-- Тексты в fts кладутся уже нормализованными (common.py:stem_text).
-- kind='note' – карточка (rowid = notes.rowid), kind='chunk' – кусок (rowid = chunks.id).
CREATE VIRTUAL TABLE IF NOT EXISTS fts_notes USING fts5(
    title, summary, keywords, questions, tokenize='unicode61 remove_diacritics 2'
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks USING fts5(
    heading, text, tokenize='unicode61 remove_diacritics 2'
);
