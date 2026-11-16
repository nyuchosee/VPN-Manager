# VPN Manager

> **Telegram-бот для управления VPN-подписками через API 3x-ui.** Заявки и продления, сроки и лимиты, HWID-устройства, тикеты поддержки и административное меню. Работает через Telegram long polling: отдельный веб-сервер и входящий порт для бота не нужны. По умолчанию изменение клиентов отключено.

## 1. Системные требования

- Linux с `systemd`, правами `sudo` и доступом к интернету; ориентир — Ubuntu/Debian с Python **3.10+** и модулем `venv`.
- Действующая панель **3x-ui** с HTTPS-доступом к API и заранее созданными Inbound (совместимость конкретной версии следует проверять диагностикой).
- Отдельный Telegram-бот и его токен от BotFather, числовой Telegram ID администратора, API Token панели, HTTPS-адрес панели и шаблон URL подписки.
- `python3`, `tar`, `systemctl`; зависимости Python устанавливаются мастером. `nginx` и Xray не переустанавливаются.
- Перед установкой/обновлением — резервная копия панели и доступ к аварийной консоли VPS.

## 2. Установка и команды

**Скачать:** замените `OWNER/REPOSITORY` адресом публичного репозитория (ветка `main`):

```bash
BASE="https://raw.githubusercontent.com/OWNER/REPOSITORY/main/dist"
wget -O vpn-manager-setup-0.5.2.sh "$BASE/vpn-manager-setup-0.5.2.sh"
wget -O SHA256SUMS "$BASE/SHA256SUMS"
sha256sum -c SHA256SUMS
bash vpn-manager-setup-0.5.2.sh --verify
```

**Установить** (или запустить безопасный мастер обновления, если бот уже установлен):

```bash
sudo bash vpn-manager-setup-0.5.2.sh
```

Мастер последовательно запросит токен Telegram, ID администратора, HTTPS URL и API Token 3x-ui, список ID существующих Inbound и шаблон подписки. Ввод секретов скрыт. При первоначальной установке выполняется резервирование панели и проверка API **без записи**. При обновлении прежняя SQLite и конфигурация сохраняются; требуется заранее выключить режим записи.

**После установки:**

```bash
sudo systemctl status vpn-bot --no-pager
sudo vpn-manager-probe
sudo vpn-manager-doctor --api
sudo journalctl -u vpn-bot -n 50 --no-pager
```

**Управление:**

```bash
sudo vpn-manager-configure           # изменить настройки; запись будет отключена
sudo vpn-manager-inbounds            # изменить Inbound для новых пользователей
sudo vpn-manager-backup --all        # резервная копия приложения и панели
sudo vpn-manager-backup --bot        # только локальные данные бота
sudo vpn-manager-write-mode         # выбрать чтение / запись
sudo vpn-manager-write-mode --disable
sudo systemctl restart vpn-bot       # перезапуск бота
sudo vpn-manager-uninstall           # удаление бота с подтверждением
```

После настройки сначала проверьте работу `/start`, `/admin` и диагностических команд на тестовом клиенте. Операции записи разрешайте только после проверки совместимости. Справочные инструкции: [установка](docs/INSTALLATION_RU.md), [администрирование](docs/OPERATIONS_RU.md), [резервные копии](docs/BACKUPS_RU.md), [безопасность](docs/SECURITY_RU.md), [публикация на GitHub](docs/PUBLISHING_RU.md).

## 3. Возможные ошибки и решения

| Ошибка или симптом | Что проверить |
|---|---|
| `203/EXEC` / `Permission denied` | Права на `/opt/vpn-bot/.venv`, запуск Python от системного пользователя `vpn-bot`: `sudo vpn-manager-doctor`. |
| Бот не отвечает на `/start` | `systemctl status vpn-bot`, журнал `journalctl -u vpn-bot`, корректность Telegram-токена, конфликт `409 Conflict` с другим процессом polling. |
| API недоступен / `401` / `403` | HTTPS URL панели, секретный base path, срок действия API Token; выполнить `sudo vpn-manager-probe`. |
| `PANEL_REJECTED` / неверный формат JSON | Совместимость полей API 3x-ui; **не повторять** спорную операцию до сверки фактического клиента в панели. |
| Inbound отсутствует или выключен | Найти ID и состояние в 3x-ui; выполнить `sudo vpn-manager-inbounds`. |
| Операция `needs_review` | Сверить состояние клиента через панель и `/ops`; не подтверждать повторно автоматически. |
| Неверная ссылка подписки | Исправить шаблон с единственным `{sub_id}` через `sudo vpn-manager-configure`. |
| Ошибка обновления | Проверить резервную копию, статус службы и `sudo vpn-manager-doctor --api`; не удалять SQLite ради переустановки. |

> Программное обеспечение предназначено для самостоятельного администрирования. Локальные тесты не гарантируют совместимость каждого API или безопасность развёрнутой инфраструктуры. Не публикуйте токены, базы, резервные копии и действующие ссылки подписок.
