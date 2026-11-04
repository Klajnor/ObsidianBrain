# Changelog

Все значимые изменения проекта. Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/).

## [Unreleased]

### Добавлено

- Автообновление кода `/opt/brain` через `kapum-update.timer` из KapumChatBot: отдельный read-only deploy-ключ и проверенный host key, только fast-forward без потери локальных правок; root-owned код/venv. Проверяется Python и настоящий MCP handshake от `brain` без LLM/записи. При сбое возвращаются затронутые коммиты и полные снимки `.venv` (индекс и заметки не откатываются). Активный `brain-sync` перезапускается; `brain-pull.timer` приостанавливается, идущий oneshot не прерывается. Подробности установки — README KapumChatBot, «Автообновление на сервере».

- `brain_status`: служебный MCP-инструмент для отчёта о статусе. Возвращает только числа — заметки, фрагменты, теги, оценённые/сильные связи и очередь неоценённых пар (`candidate_pairs`), — без путей и текста заметок. Бот вызывает его без аргументов и не показывает модели; БД открывается read-only.
- Понятные имена и ключевые теги Telegram-заметок: `create_note` принимает `title` и `tags` от клиента, очищает имя до ru/en букв и цифр, ограничивает 48 символами без `.md` вместе с уникальным суффиксом и разрешает коллизии без перезаписи. YAML `tags` сразу индексируются в `note_tags`/`keywords`/FTS, исходные теги и текст сохраняются. Полный хеш request_id во frontmatter позволяет повторять сохранение с другими AI-метаданными после сбоя индекса/Git без дубликатов; старые hash-имена не меняются. Сам MCP по-прежнему не вызывает LLM.
- Связность заметок: `tags.py` извлекает теги Obsidian (frontmatter `tags`/`tag` и инлайновые `#теги`), `sync.py` хранит их в `note_tags`; пары с общими тегами, а для заметок без теговых пар — с общими `keywords`/`entities`, сравниваются ИИ по готовым `summary`, результат (`score`, общие темы, объяснение) кэшируется в `note_links`. Лимиты — `BRAIN_LINKS_ENABLED`, `BRAIN_LINK_MAX_PAIRS`, `BRAIN_LINK_MAX_PER_NOTE`, `BRAIN_LINK_MAX_TAG_FANOUT`, `BRAIN_LINK_MIN_SHARED_TERMS`, `BRAIN_LINK_MAX_TERM_FANOUT`, `BRAIN_LINK_MIN_SCORE`.
- MCP: `search_notes` возвращает теги и до 3 связанных заметок в карточке (`include_related=false` отключает), добавлен инструмент `get_related(path, k, min_score)`. Связи выдаются только выше `BRAIN_LINK_MIN_SCORE`.
- `capture.py` и MCP-инструмент `create_note`: создание только новых заметок в `Telegram/` первого хранилища при `BRAIN_WRITE_ENABLED=true`, без LLM и без перезаписи существующих файлов.
- `git_publish.py`: публикация созданной Telegram-заметки отдельным коммитом и push точного SHA в upstream без force/тегов/submodules.
- `install-systemd.sh`: копирование `brain-sync`/`brain-pull` в `/etc/systemd/system`, `daemon-reload` и выбор варианта `sync`/`pull`/`all`.
- Тесты `tests/test_capture.py`, `tests/test_git_publish.py`, `tests/test_links.py` и `tests/test_mcp_server.py` на временном хранилище/БД и локальных bare-репозиториях.
- Полный пересчёт индекса без удаления БД: `sync.py --reindex` переобрабатывает все заметки, игнорируя `content_hash`, `--rebuild-links` очищает оценки и счётчики ошибок связей; вместе — штатный способ пересборки после смены LLM-модели.
- `brain_status` показывает `failed_links` — пары, отложенные после `BRAIN_LINK_MAX_ATTEMPTS` ошибок.

### Исправлено

- `brain-pull.service`: добавлен защищённый `StateDirectory=brain-git` (`/var/lib/brain-git`, `0700`) для HTTPS credentials и файла блокировки `credential-store`. В README описан перенос credentials и сброс унаследованного helper; `ProtectHome=read-only` и `ProtectSystem=strict` сохранены. Обновление одного юнита без переключения helper на новый путь не исправляет старую конфигурацию.
- Ошибки `в ответе модели нет JSON-объекта` при пустом/обрезанном ответе LLM: `classify_pair` распознаёт `finish_reason=length` и пустой `content`, повторяет запрос с удвоенным лимитом токенов (`LLM_LINK_RETRIES`, `LLM_LINK_MAX_TOKENS_CAP`), а ошибки ответа учитываются в новой таблице `link_failures`. После `BRAIN_LINK_MAX_ATTEMPTS` (по умолчанию 3) пара откладывается и перестаёт занимать очередь каждый прогон; успешная оценка или `--rebuild-links` счётчик сбрасывают.
- Удаление заметки теперь явно чистит `chunks`, `note_tags`, `note_links`, `link_failures` и FTS, не полагаясь только на `ON DELETE CASCADE`; в начале каждого прогона `prune_orphans` убирает «сирот», оставшихся от старых версий или БД с выключенными внешними ключами. Удалённая заметка больше не находится поиском и не остаётся в связях.

### Безопасность

- `brain-sync.service` и `brain-pull.service` теперь выставляют `UMask=0077`: `data/brain.db` (полный текст заметок, включая `-wal`/`-shm`) и `.env` с ключом API создаются с правами `600`, каталог `data/` — `700`. Для уже существующих файлов нужен разовый `chmod 700 data && chmod 600 .env data/brain.db*` (добавлено в README).
- MCP-сервер открывает `data/brain.db` в режиме `mode=ro` + `PRAGMA query_only=ON`, без DDL, WAL и создания файла. Инструменты чтения больше не могут изменить индекс; запись остаётся только у `sync.py` и `create_note`. Если БД ещё нет, возвращается понятная ошибка вместо создания пустой базы.
- MCP-сервер не загружает `LLM_API_KEY` из `.env` (`BRAIN_ENV_SKIP`) и удаляет его из своего окружения: поиску ключ LLM не нужен, меньше риск утечки при компрометации MCP-клиента.
- `iter_notes()` не обходит симлинки/junction и проверяет resolved-путь на принадлежность хранилищу: ссылка больше не может подсунуть в индекс (и в LLM) файл снаружи, а junction-петли не создают дубли и лишние LLM-вызовы. Повторные пути схлопываются по resolved-пути.
- Ограничены размеры входов: заметки больше `LLM_MAX_NOTE_BYTES` (5 МБ) не индексируются, map-reduce делает не больше `LLM_MAX_CHUNKS` (60) LLM-вызовов на заметку; в MCP `query` обрезается до 500 символов, `k` — до 50, `get_note` отдаёт только обычные файлы до 1 МБ (FIFO/каталоги отклоняются). Это защищает от расходов и зависаний на больших или специальных файлах.
- `LLM_BASE_URL` по `http://` вне `localhost`/`127.0.0.1`/`::1` теперь отклоняется: иначе ключ API и текст заметок передавались бы открытым текстом. Для LAN-сервера по HTTP есть явный override `LLM_ALLOW_HTTP=1` с предупреждением.
- Пути в логах `sync.py` печатаются через `repr`, а заголовок Telegram-заметки очищается от управляющих символов: имя файла или первая строка сообщения больше не могут вставить ANSI-последовательности/переводы строк в терминал и journald.
- `VAULT_PATH` больше не принимает корень файловой системы (`/`, `C:\`): иначе индексировалась бы вся ФС, а `get_note` мог бы отдать любой `.md` на диске.
- `.gitignore` не даёт случайно закоммитить `.env.*`, `*.db`/`*.db-wal`/`*.db-shm`, `.venv`; в `requirements.txt` добавлены верхние границы мажорных версий зависимостей.
- systemd-юниты дополнительно ограничены (`PrivateDevices`, `ProtectKernel*`, `RestrictNamespaces`/`Realtime`/`SUIDSGID`, `LockPersonality`, пустой `CapabilityBoundingSet`, `RestrictAddressFamilies`). `brain-pull.service` не зависает на запросе логина (`GIT_TERMINAL_PROMPT=0`) и имеет `TimeoutStartSec=2h` вместо бесконечного ожидания.
- `sync.py` завершается с кодом 1, если хотя бы одна заметка не проиндексировалась. Раньше ошибки только печатались, а systemd/таймер видели успех и молчали о сломанном индексе.
- В README и AGENTS зафиксированы границы доверия: заметки и их имена — недоверенные данные (prompt injection), MCP не аутентифицирует клиента, авто-pull remote может доставить `.mcp.json`/`.obsidian/plugins`, `create_note` требует клиентской авторизации и уникального `request_id` с namespace пользователя/чата.
- При `BRAIN_WRITE_ENABLED=false` инструмент `create_note` не регистрируется в MCP вообще (модуль `capture` не импортируется): у общего/читающего профиля нет даже схемы инструмента записи, которую можно было бы вызвать через prompt injection.
