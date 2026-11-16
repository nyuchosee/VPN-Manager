#!/usr/bin/env bash
# VPN Manager clean uninstall. Removes ONLY this bot, not 3x-ui/Xray/nginx.
set -Eeuo pipefail
umask 077
[[ $EUID -eq 0 ]] || { echo 'Необходим sudo/root.' >&2; exit 1; }
[[ -r /dev/tty ]] || { echo 'Требуется интерактивная SSH-консоль.' >&2; exit 1; }
DEST=/opt/vpn-bot
CFG_DIR=/etc/vpn-bot
DATA_DIR=/var/lib/vpn-bot
UNIT=/etc/systemd/system/vpn-bot.service
BACKUPS=/var/backups/vpn-manager
MARKER="$DEST/.vpn-manager-install-id"
[[ -f "$MARKER" && ! -L "$MARKER" ]] || {
  echo 'Маркер установки не найден; удаление остановлено, чтобы не затронуть чужие файлы.' >&2; exit 1;
}
version="$(cat "$MARKER")"
if [[ ! "$version" =~ ^telegram-vpn-manager:v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo 'Неизвестный формат маркера VPN Manager. Удаление остановлено.' >&2
  exit 1
fi
for p in "$DEST" "$CFG_DIR" "$DATA_DIR"; do
  if [[ -L "$p" ]]; then echo "Путь $p — символическая ссылка. Автоматическое удаление остановлено." >&2; exit 1; fi
done
if [[ -f "$UNIT" ]]; then
  [[ ! -L "$UNIT" ]] || { echo 'Служба представляет собой символическую ссылку. Остановка.' >&2; exit 1; }
  grep -qF 'ExecStart=/opt/vpn-bot/.venv/bin/python -m app.main' "$UNIT" || {
    echo 'Содержимое vpn-bot.service отличается от ожидаемого. Удаление остановлено.' >&2; exit 1;
  }
fi
cat >/dev/tty <<MSG

============================================================
 VPN Manager — ПОЛНАЯ ОЧИСТКА ЛОКАЛЬНОЙ УСТАНОВКИ
============================================================
Обнаружено: $version

Будут удалены:
  - /opt/vpn-bot (программа и Python-окружение)
  - /etc/vpn-bot (включая токены)
  - /var/lib/vpn-bot (SQLite, привязки, заявки, журнал)
  - служба vpn-bot.service и команды vpn-manager-*
  - системный пользователь vpn-bot (если создан установщиком)

НЕ будут изменены:
  - 3x-ui и её пользователи, подписки, UUID и subId
  - nginx, Xray, настройки маршрутизации/DNS, правила UFW/SSH
  - хранящиеся отдельно резервные копии

Перед удалением будет создан НОВЫЙ резервный архив бота.
MSG
printf 'Для удаления введите УДАЛИТЬ-VPN-MANAGER: ' >/dev/tty
IFS= read -r confirm </dev/tty
[[ "$confirm" == 'УДАЛИТЬ-VPN-MANAGER' ]] || { echo 'Отмена.'; exit 0; }
BACKUP_TOOL=/usr/local/sbin/vpn-manager-backup
if [[ -x "$BACKUP_TOOL" ]]; then
  "$BACKUP_TOOL" --bot
elif [[ -f "$DEST/app/storage.py" && -f "$DEST/scripts/backup.sh" ]]; then
  bash "$DEST/scripts/backup.sh" --bot
else
  echo 'Не найдена штатная команда резервирования VPN Manager. Удаление прервано.' >&2
  exit 1
fi
# A successful backup must be completed before touching any deployed files.
if systemctl cat vpn-bot.service >/dev/null 2>&1; then
  systemctl disable --now vpn-bot.service || { echo 'Не удалось остановить vpn-bot.' >&2; exit 1; }
fi
rm -f -- "$UNIT"
systemctl daemon-reload
systemctl reset-failed vpn-bot.service >/dev/null 2>&1 || true
rm -rf -- "$DEST" "$CFG_DIR" "$DATA_DIR"
for cmd in backup configure probe uninstall write-mode inbounds doctor; do
  rm -f -- "/usr/local/sbin/vpn-manager-$cmd"
done
if id vpn-bot >/dev/null 2>&1; then
  shell="$(getent passwd vpn-bot | cut -d: -f7)"
  home="$(getent passwd vpn-bot | cut -d: -f6)"
  if [[ "$shell" == /usr/sbin/nologin && "$home" == /var/lib/vpn-bot ]]; then
    userdel vpn-bot || { echo 'Не удалось удалить vpn-bot. Перед новой установкой проверьте: getent passwd vpn-bot' >&2; exit 1; }
  else
    echo 'Пользователь vpn-bot оставлен (не соответствует созданному установщиком).'
    echo 'Новая установка потребует проверки и удаления конфликта вручную.' >&2
  fi
fi
echo 'Локальная установка бота удалена. VPN-клиенты в 3x-ui сохранены.'
echo "Архивы прежнего бота находятся в $BACKUPS"
printf 'Удалить также резервные копии в %s? [y/N]: ' "$BACKUPS" >/dev/tty
IFS= read -r purge </dev/tty
if [[ "$purge" == y || "$purge" == Y ]]; then
  printf 'Для подтверждения необратимого удаления введите УДАЛИТЬ-БЭКАПЫ: ' >/dev/tty
  IFS= read -r phrase </dev/tty
  if [[ "$phrase" == 'УДАЛИТЬ-БЭКАПЫ' ]]; then
    if [[ -d "$BACKUPS" && ! -L "$BACKUPS" ]]; then
      rm -rf -- "$BACKUPS"
      echo 'Локальные бэкапы VPN Manager удалены.'
    else
      echo 'Каталог резервных копий не найден либо является символической ссылкой; не удаляем.'
    fi
  else
    echo 'Бэкапы сохранены.'
  fi
fi
echo 'Теперь можно запускать свежий мастер установки.'
