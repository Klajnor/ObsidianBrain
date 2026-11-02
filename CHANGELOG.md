# Changelog

Все значимые изменения проекта. Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/).

## [Unreleased]

### Добавлено

- `capture.py` и MCP-инструмент `create_note`: создание только новых заметок в `Telegram/` первого хранилища при `BRAIN_WRITE_ENABLED=true`, без LLM и без перезаписи существующих файлов.
- Тесты `tests/test_capture.py` на временном хранилище и БД.
