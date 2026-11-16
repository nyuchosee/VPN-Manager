#!/usr/bin/env bash
# Transactional update of VPN Manager only. NEVER reinstall 3x-ui or edit Xray/nginx.
set -Eeuo pipefail
umask 077
[[ $EUID -eq 0 ]] || { echo 'Запускайте от root через sudo.' >&2; exit 1; }
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(cat "$ROOT/VERSION")"
DEST=/opt/vpn-bot
CFG=/etc/vpn-bot/bot.env
UNIT=/etc/systemd/system/vpn-bot.service
if [[ ! -d "$DEST/app" || ! -f "$CFG" || ! -f "$UNIT" ]]; then
  echo 'Действующая установка VPN Manager не найдена; используйте мастер первоначальной установки.' >&2
  exit 1
fi
id vpn-bot >/dev/null || { echo 'Нет системного пользователя vpn-bot.' >&2; exit 1; }
# Ensure previous data state is not readable by unrelated OS accounts.
chown vpn-bot:vpn-bot /var/lib/vpn-bot
chmod 0700 /var/lib/vpn-bot
for dbpart in /var/lib/vpn-bot/bot.sqlite3 /var/lib/vpn-bot/bot.sqlite3-wal /var/lib/vpn-bot/bot.sqlite3-shm; do
  if [[ -f "$dbpart" ]]; then chmod 0600 "$dbpart"; fi
done
[[ -r /dev/tty ]] || { echo 'Обновление требует интерактивный SSH-терминал.' >&2; exit 1; }
oldversion="$(cat "$DEST/.vpn-manager-install-id" 2>/dev/null || echo unknown)"
echo "Установлено: $oldversion"
echo "Обновить до версии $VERSION? Будет сохранена существующая конфигурация и SQLite."
printf 'Для продолжения введите ОБНОВИТЬ: ' >/dev/tty
IFS= read -r confirm </dev/tty
[[ "$confirm" == 'ОБНОВИТЬ' ]] || { echo 'Обновление отменено.'; exit 1; }

# No external mutations are allowed until the version is verified with a test client.
if ! grep -qx 'WRITE_ENABLED=false' "$CFG"; then
  echo 'Предупреждение: сейчас режим записи не подтверждён как выключенный.'
  echo 'Перед обновлением выполните: sudo vpn-manager-write-mode --disable' >&2
  exit 1
fi
command -v runuser >/dev/null || { echo 'Нет команды runuser.' >&2; exit 1; }
command -v python3 >/dev/null || { echo 'Нет python3.' >&2; exit 1; }
command -v systemctl >/dev/null || { echo 'Нет systemctl.' >&2; exit 1; }

printf '\nСоздаём резервные копии ПЕРЕД изменением файлов...\n'
if [[ -x /usr/local/sbin/vpn-manager-backup ]]; then
  /usr/local/sbin/vpn-manager-backup --all
else
  echo 'Отсутствует старая команда бэкапа; автоматический безопасный апдейт невозможен.' >&2
  exit 1
fi
stamp="$(date -u +%Y%m%dT%H%M%SZ)-$$"
ROLLBACK="/var/backups/vpn-manager/upgrade-$stamp"
install -d -m 0700 "$ROLLBACK"
cp -a "$DEST/app" "$DEST/.venv" "$UNIT" "$CFG" "$ROLLBACK/"
[[ -f "$DEST/requirements.txt" ]] && cp -a "$DEST/requirements.txt" "$ROLLBACK/" || true
[[ -f "$DEST/INSTALL_RU.md" ]] && cp -a "$DEST/INSTALL_RU.md" "$ROLLBACK/" || true
[[ -f "$DEST/.vpn-manager-install-id" ]] && cp -a "$DEST/.vpn-manager-install-id" "$ROLLBACK/" || true
install -d -m 0700 "$ROLLBACK/commands"
for cmd in backup configure probe uninstall write-mode inbounds doctor; do
  if [[ -f "/usr/local/sbin/vpn-manager-$cmd" ]]; then
    cp -a "/usr/local/sbin/vpn-manager-$cmd" "$ROLLBACK/commands/"
  fi
done

STAGE="$(mktemp -d "$DEST/.upgrade-stage.XXXXXXXX")"
# A root-created virtual environment under umask 077 was the source of 203/EXEC.
# Group traverse+execute is deliberately granted before any service starts.
cp -a "$ROOT/app" "$STAGE/app"
cp -a "$ROOT/requirements.txt" "$STAGE/requirements.txt"
python3 -m venv "$STAGE/.venv"
"$STAGE/.venv/bin/python" -m pip install -q --disable-pip-version-check -r "$STAGE/requirements.txt"
chown -R root:vpn-bot "$STAGE"
chmod 0750 "$STAGE"
chmod -R g+rX "$STAGE/app" "$STAGE/.venv"
cd "$STAGE"
runuser -u vpn-bot -- "$STAGE/.venv/bin/python" --version
runuser -u vpn-bot -- "$STAGE/.venv/bin/python" -c 'import aiogram,httpx,app.main'
echo 'Проверка запуска Python от vpn-bot: OK'

WAS_ACTIVE=0
if systemctl is-active --quiet vpn-bot; then WAS_ACTIVE=1; fi
systemctl stop vpn-bot || true
SWAPPED=0
finish(){
  local rc=$?
  if [[ $rc -ne 0 && $SWAPPED -eq 1 ]]; then
    echo 'Ошибка обновления. Откат файлов VPN Manager...' >&2
    systemctl stop vpn-bot || true
    rm -rf "$DEST/app" "$DEST/.venv"
    cp -a "$ROLLBACK/app" "$ROLLBACK/.venv" "$DEST/"
    [[ -f "$ROLLBACK/requirements.txt" ]] && cp -a "$ROLLBACK/requirements.txt" "$DEST/" || true
    cp -a "$ROLLBACK/vpn-bot.service" "$UNIT"
    cp -a "$ROLLBACK/bot.env" "$CFG"
    for script in "$ROLLBACK"/commands/vpn-manager-*; do
      [[ -f "$script" ]] && cp -a "$script" /usr/local/sbin/ || true
    done
    [[ -f "$ROLLBACK/INSTALL_RU.md" ]] && cp -a "$ROLLBACK/INSTALL_RU.md" "$DEST/" || true
    [[ -f "$ROLLBACK/.vpn-manager-install-id" ]] && cp -a "$ROLLBACK/.vpn-manager-install-id" "$DEST/" || true
    systemctl daemon-reload || true
    if [[ $WAS_ACTIVE -eq 1 ]]; then systemctl start vpn-bot || true; fi
    echo "Откат выполнен. Копия: $ROLLBACK" >&2
  fi
  rm -rf "$STAGE"
  exit $rc
}
trap finish EXIT
SWAPPED=1
rm -rf "$DEST/app" "$DEST/.venv"
cp -a "$STAGE/app" "$DEST/"
cp -a "$ROOT/requirements.txt" "$DEST/requirements.txt"
# Virtual environments are not reliably relocatable; build at the FINAL path.
python3 -m venv "$DEST/.venv"
"$DEST/.venv/bin/python" -m pip install -q --disable-pip-version-check -r "$ROOT/requirements.txt"
chown -R root:vpn-bot "$DEST/app" "$DEST/.venv"
chmod -R g+rX "$DEST/app" "$DEST/.venv"
install -o root -g root -m 0644 "$ROOT/deploy/vpn-bot.service" "$UNIT"
install -o root -g root -m 0644 "$ROOT/INSTALL_RU.md" "$DEST/INSTALL_RU.md"
for cmd in backup configure probe uninstall write-mode inbounds doctor; do
  install -o root -g root -m 0755 "$ROOT/scripts/$cmd.sh" "/usr/local/sbin/vpn-manager-$cmd"
done
printf 'telegram-vpn-manager:v%s\n' "$VERSION" >"$DEST/.vpn-manager-install-id"
chmod 0644 "$DEST/.vpn-manager-install-id"
systemctl daemon-reload
runuser -u vpn-bot -- "$DEST/.venv/bin/python" --version
(cd "$DEST" && runuser -u vpn-bot -- "$DEST/.venv/bin/python" -c 'import aiogram,httpx,app.main')
/usr/local/sbin/vpn-manager-doctor --api
if [[ $WAS_ACTIVE -eq 1 ]]; then
  systemctl reset-failed vpn-bot || true
  systemctl start vpn-bot
  sleep 3
  systemctl is-active --quiet vpn-bot || { echo 'Новая служба не запустилась; откат.' >&2; exit 1; }
  echo 'Бот работает после обновления.'
else
  echo 'Бот до обновления был остановлен. Он оставлен остановленным.'
  echo 'После проверки: sudo systemctl start vpn-bot'
fi
SWAPPED=0
echo "Обновление до $VERSION завершено. Откатная копия: $ROLLBACK"
echo 'WRITE_ENABLED=false сохранён; вручную проверяйте операции только на тестовом клиенте.'
