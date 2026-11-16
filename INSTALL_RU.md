# Установка и обновление VPN Manager

Настоящая инструкция применима к автономному установщику из каталога `dist/`. Она не содержит параметров конкретного сервера.

1. Подготовьте работающую панель 3x-ui с HTTPS API, отдельного Telegram-бота, ID администраторов, включённые Inbound и ссылку подписки с `{sub_id}`. Убедитесь, что на машине доступны `systemd`, Python 3.10+ и `sudo`.
2. Создайте резервную копию 3x-ui. Если бот уже установлен, предварительно выполните `sudo vpn-manager-write-mode --disable` и `sudo vpn-manager-backup --all`.
3. Скачайте `dist/vpn-manager-setup-0.5.2.sh` и `dist/SHA256SUMS` из одного коммита репозитория и проверьте: `sha256sum -c SHA256SUMS`.
4. Проверьте встроенный архив: `bash vpn-manager-setup-0.5.2.sh --verify`.
5. Запустите мастер: `sudo bash vpn-manager-setup-0.5.2.sh` и следуйте приглашениям терминала.
6. Убедитесь, что работает `sudo systemctl status vpn-bot --no-pager` и диагностика `sudo vpn-manager-doctor --api`.
7. Проверьте функции бота на отдельной тестовой записи 3x-ui. Для изменения клиентов запись разрешается отдельно через `sudo vpn-manager-write-mode`.

Повторный запуск файла обнаруживает установленный экземпляр и предлагает обновление, сохраняя конфигурацию и SQLite. Рабочую базу не удаляйте. Установщик не создаёт публичных портов Telegram и не переустанавливает 3x-ui/Xray/nginx.

Подробнее: `docs/INSTALLATION_RU.md`, `docs/OPERATIONS_RU.md`, `docs/BACKUPS_RU.md` и `docs/SECURITY_RU.md`.
