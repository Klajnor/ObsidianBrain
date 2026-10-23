# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

«Второй мозг» для Obsidian-хранилища, которое лежит **снаружи** этого репозитория (путь – `VAULT_PATH` в `.env`, сейчас `d:\-=Obsidian GIT=-\Obsidian GIT`). Python-код, который:

1. дистиллирует каждую заметку хранилища через OpenAI-совместимое API в карточку (описание, ключевые слова, сущности, вопросы, тип) и чанки по заголовкам;
2. хранит их в SQLite с FTS5 (`data/brain.db`, пересобирается из заметок, в git не попадает);
3. отдаёт поиск чату через MCP-сервер.

Хранилище для этого кода – только источник данных: **заметки отсюда не изменять**, только читать. Сборки, линтера и тестов нет.

## Layout

- `common.py` – `BRAIN_DIR`, `VAULT`, `DB_PATH`, `load_env()` (вызывается при импорте), подключение к БД, нормализация текста (`stem_text`, `fts_query`, PyStemmer для ru/en), `iter_notes()` с исключениями.
- `sync.py` – инкрементальная дистилляция: SHA-256 содержимого, новые/изменённые → LLM → upsert, исчезнувшие → удаление. Длинные заметки (> `LLM_MAX_INPUT_CHARS`) суммаризируются по разделам (map-reduce). Флаги `--dry-run`, `--watch` (watchdog, debounce 30 с).
- `mcp_server.py` – FastMCP-сервер `brain`: `search_notes`, `get_chunk`, `get_note`, `list_notes`. Регистрируется в `.mcp.json` **хранилища**, а не здесь.
- `schema.sql` – таблицы `notes`, `chunks`, FTS5 `fts_notes`, `fts_chunks`. В FTS кладётся уже нормализованный (stem) текст.
- `prompt.md` – системный промпт дистилляции; к нему в `sync.py` добавляется `JSON_HINT`.
- `.env.example` – образец настроек с расшифровкой параметров.

## Commands

- `pip install -r requirements.txt`
- `python sync.py --dry-run` – что будет обработано, без вызовов LLM
- `python sync.py` – один проход; `python sync.py --watch` – непрерывно
- `python mcp_server.py` – запуск MCP-сервера (обычно его запускает клиент по `.mcp.json`)

## Config and secrets

- `.env` (gitignored) содержит `VAULT_PATH`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` и необязательные `LLM_*`. Никогда не читать, не печатать и не коммитить; менять только `.env.example`.
- Переменные окружения приоритетнее `.env`.
- Не заводить прямую зависимость от конкретного провайдера: используется только пакет `openai` с `base_url`. Структурированный вывод – JSON из текста ответа (`parse_card`), без tool calling.

## Conventions

- Пути заметок в БД – относительные, с `/`, UTF-8; русские имена не транслитерировать.
- Изменения схемы БД: индекс пересобираемый, поэтому достаточно поправить `schema.sql` и удалить `data/brain.db`.
- Комментарии и сообщения в коде – на русском, как в существующем коде.
- Не коммитить без просьбы пользователя.
