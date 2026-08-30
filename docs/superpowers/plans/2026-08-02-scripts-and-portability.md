# Скрипты, выбор аккаунтов и переносимость — план

> Спека: `docs/superpowers/specs/2026-08-02-scripts-and-portability-design.md`.
> План намеренно компактный: код не дублируется, потому что исполняется тем же агентом сразу следом. Каждая задача заканчивается прогоном `python3 -m unittest discover -s tests`.

**Цель:** проект можно отдать другому человеку; типовые операции запускаются кликом; в настройках выбираются аккаунты для открытия и для шаринга чатов.

---

### Задача 1 — реестр: списки аккаунтов и миграция

**Файлы:** `claude_login/store.py`, тесты `TestAccountSelection`.

- Ключи `appOpenAccounts`, `appSharedAccounts`; правило «ключа нет = все».
- Аксессоры `app_open_accounts()` / `app_shared_accounts()` → `Optional[list[str]]`, сеттеры.
- `shares_chats(profile) -> bool` и `sharing_account_uuids() -> Optional[set[str]]` (None = все).
- Миграция в `load()`: `appSessionsShared: false` → `appSharedAccounts: []`; `true` → ключ не создаётся.
- Удалить `app_sessions_shared` / `set_app_sessions_shared`, поправить все места вызова.

Тесты: отсутствие ключа = все; пустой список = никто; миграция false; несуществующее имя в списке не ломает выборку.

### Задача 2 — шаринг как свойство аккаунта

**Файлы:** `claude_login/store.py`, `claude_login/commands.py`, тесты `TestPerAccountSharing`.

- `wire_session_pool(..., sharing: Optional[set[str]] = None)` — лист пропускается, если `leaf.parent.name` не в `sharing`.
- `commands` передаёт `vault.sharing_account_uuids()` во всех вызовах.
- `link_session_pool` вызывается только для профиля, у которого стоит галочка.
- Снятие галочки → `unwire_session_pool` для этого профиля, но не для каталога, открытого в приложении.

Тесты: лист аккаунта без галочки не уходит в пул даже в машинном каталоге; с галочкой уходит; снятие возвращает чаты.

### Задача 3 — команды `app open`, `sync --gather`, `sync-all`, `setup`

**Файлы:** `claude_login/commands.py`, `claude_login/cli.py`, `claude_login/store.py`, тесты `TestAppOpen`, `TestGatherMcp`, `TestSyncAll`, `TestSetup`.

- `cmd_app_open`: имена из argv → `appOpenAccounts` → все; пропуск открытых; `LAUNCH_STAGGER = 1.5`.
- `Vault.gather_config`: собрать `mcpServers` из профилей в машинный конфиг, только добавляя отсутствующее; вернуть список добавленных имён.
- `cmd_sync` получает `--gather`.
- `cmd_sync_all`: relink → sync --gather → app relink → app link → app adopt; шаги не прерывают друг друга, итог суммируется, код возврата ненулевой при сбое.
- `cmd_setup`: проверки, список аккаунтов, цикл `add`, выбор цели, `sync-all`, подсказка про `scripts/`. Без TTY — только печать состояния.

### Задача 4 — экраны настроек со списками

**Файлы:** `claude_login/commands.py`, тесты `TestSettingsSections`.

- Строку `Share chats & projects` заменить на `Accounts sharing chats` со счётчиком `N of M`.
- Добавить `Accounts to open` со счётчиком.
- Общий экран-список `_account_picker(vault, title, selected_names)`: Enter переключает галочку, возвращает `True` для перерисовки.

### Задача 5 — скрипты и install.sh

**Файлы:** `scripts/setup.command`, `scripts/open-accounts.command`, `scripts/sync-all.command`, `scripts/doctor.command`, `install.sh`, тест `TestScripts`.

- Одинаковая рамка: шебанг, `set -euo pipefail`, `cd "$(dirname "${BASH_SOURCE[0]}")/.."`, вызов, пауза.
- `install.sh`: `chmod +x scripts/*.command`.

### Задача 6 — сторож приватности и уборка

**Файлы:** тесты `TestNoPrivateData`, удаление `.DS_Store`.

- Скан файлов репозитория на почты вне плейсхолдерных доменов, UUID вне белого списка, пути `/Users/` кроме `/Users/someone`.

### Задача 7 — README и AGENTS.md

- README: «Первый запуск» с нуля, раздел про скрипты, про выбор аккаунтов, про приватность («креды вставлять никуда не нужно»), про коннекторы, которые перенести нельзя, про Gatekeeper.
- AGENTS.md: новые ключи реестра, `sharing`-параметр, каталог `scripts/`, сторож приватности, обновить счётчик тестов.
