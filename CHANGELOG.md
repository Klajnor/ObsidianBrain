# Changelog

Все значимые изменения проекта. Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/).

## [Unreleased]

### Добавлено

- `capture.py` и MCP-инструмент `create_note`: создание только новых заметок в `Telegram/` первого хранилища при `BRAIN_WRITE_ENABLED=true`, без LLM и без перезаписи существующих файлов.
- Тесты `tests/test_capture.py` на временном хранилище и БД.

### Безопасность

- `brain-sync.service` и `brain-pull.service` теперь выставляют `UMask=0077`: `data/brain.db` (полный текст заметок, включая `-wal`/`-shm`) и `.env` с ключом API создаются с правами `600`, каталог `data/` — `700`. Для уже существующих файлов нужен разовый `chmod 700 data && chmod 600 .env data/brain.db*` (добавлено в README).
- MCP-сервер открывает `data/brain.db` в режиме `mode=ro` + `PRAGMA query_only=ON`, без DDL, WAL и создания файла. Инструменты чтения больше не могут изменить индекс; запись остаётся только у `sync.py` и `create_note`. Если БД ещё нет, возвращается понятная ошибка вместо создания пустой базы.
