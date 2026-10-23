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

-- Тексты в fts кладутся уже нормализованными (brain/common.py:stem_text).
-- kind='note' – карточка (rowid = notes.rowid), kind='chunk' – кусок (rowid = chunks.id).
CREATE VIRTUAL TABLE IF NOT EXISTS fts_notes USING fts5(
    title, summary, keywords, questions, tokenize='unicode61 remove_diacritics 2'
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks USING fts5(
    heading, text, tokenize='unicode61 remove_diacritics 2'
);
