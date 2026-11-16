#!/usr/bin/env bash
# Interactive wizard, safe initial deployment. Does not reinstall or restart 3x-ui/nginx/Xray.
set -Eeuo pipefail
umask 077
[[ "$EUID" -eq 0 ]] || { echo 'Запустите: sudo bash scripts/install.sh' >&2; exit 1; }
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "${VPN_MANAGER_BANNER_SHOWN:-}" != 1 ]]; then bash "$ROOT/scripts/banner.sh"; fi
[[ -r /dev/tty ]] || { echo 'Для мастера установки нужен интерактивный SSH-терминал (TTY).' >&2; exit 1; }
for tool in python3 systemctl tar; do command -v "$tool" >/dev/null || { echo "Не найден $tool" >&2; exit 1; }; done
python3 - <<'PY'
import sys
if sys.version_info < (3,10): raise SystemExit('Требуется Python 3.10+, обновите системный Python отдельно.')
PY
if [[ -e /opt/vpn-bot/app || -e /etc/vpn-bot/bot.env || -e /etc/systemd/system/vpn-bot.service ]]; then
  echo 'Обнаружена существующая установка — запускается безопасное обновление.'
  exec bash "$ROOT/scripts/upgrade.sh"
fi
cat >/dev/tty <<'MSG'

============================================================
 VPN Manager — установка для тестирования
============================================================
Не будет переустановки 3x-ui, правок nginx, Xray, настройки маршрутизации/DNS.
Один клиент может получить несколько inbound с одной подпиской.
Сначала заполним данные, затем сделаем бэкап 3x-ui,
установим зависимости и проверим API без операций записи.
MSG
STAGED="$(mktemp -d /tmp/vpn-manager-install.XXXXXXXX)"
cleanup(){ rm -rf "$STAGED"; }
trap cleanup EXIT
bash "$ROOT/scripts/configure.sh" --output "$STAGED/bot.env"

if [[ -f /etc/x-ui/x-ui.db ]]; then
  XUI_DB_PATH=/etc/x-ui/x-ui.db
else
  printf 'Не найдена стандартная /etc/x-ui/x-ui.db.\nВведите абсолютный путь к фактической SQLite базе 3x-ui: ' >/dev/tty
  IFS= read -r XUI_DB_PATH </dev/tty
  [[ "$XUI_DB_PATH" == /* && -f "$XUI_DB_PATH" ]] || { echo 'Нет базы по указанному пути. Установка остановлена.' >&2; exit 1; }
fi
export XUI_DB_PATH
printf '\nСоздаём безопасную онлайн-копию 3x-ui (без остановки службы)...\n'
bash "$ROOT/scripts/backup.sh" --panel

if ! python3 -m venv "$STAGED/check-venv" >/dev/null 2>&1; then
  if command -v apt-get >/dev/null; then
    printf 'Нужен системный пакет python3-venv. Разрешить установку через apt? [y/N]: ' >/dev/tty
    IFS= read -r answer </dev/tty
    [[ "$answer" == y || "$answer" == Y ]] || { echo 'Не установлено python3-venv.' >&2; exit 1; }
    apt-get update && apt-get install -y python3-venv
  else
    echo 'Установите пакет python3-venv самостоятельно и повторите запуск.' >&2;exit 1
  fi
fi

if ! id vpn-bot >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/vpn-bot --shell /usr/sbin/nologin vpn-bot
else
  echo 'Системный пользователь vpn-bot уже существует. Требуется ручная проверка перед установкой.' >&2
  exit 1
fi
install -d -o root -g root -m 0755 /opt/vpn-bot
install -d -o vpn-bot -g vpn-bot -m 0700 /var/lib/vpn-bot
install -d -o root -g vpn-bot -m 0750 /etc/vpn-bot
cp -R "$ROOT/app" /opt/vpn-bot/app
cp "$ROOT/requirements.txt" /opt/vpn-bot/requirements.txt
python3 -m venv /opt/vpn-bot/.venv
# Install with private umask, then explicitly provide group read/execute
# before running the service (fixes the previously observed 203/EXEC).
/opt/vpn-bot/.venv/bin/python -m pip install --disable-pip-version-check -r /opt/vpn-bot/requirements.txt
/opt/vpn-bot/.venv/bin/python -m compileall -q /opt/vpn-bot/app
chown -R root:vpn-bot /opt/vpn-bot/app /opt/vpn-bot/.venv
chmod -R g+rX /opt/vpn-bot/app /opt/vpn-bot/.venv
runuser -u vpn-bot -- /opt/vpn-bot/.venv/bin/python --version
(cd /opt/vpn-bot && runuser -u vpn-bot -- /opt/vpn-bot/.venv/bin/python -c 'import aiogram,httpx,app.main')
chown root:vpn-bot "$STAGED/bot.env"
chmod 0640 "$STAGED/bot.env"
install -o root -g vpn-bot -m 0640 "$STAGED/bot.env" /etc/vpn-bot/bot.env
install -o root -g root -m 0644 "$ROOT/deploy/vpn-bot.service" /etc/systemd/system/vpn-bot.service
for cmd in backup configure probe uninstall write-mode inbounds doctor; do
  install -o root -g root -m 0755 "$ROOT/scripts/$cmd.sh" "/usr/local/sbin/vpn-manager-$cmd"
done
install -o root -g root -m 0644 "$ROOT/INSTALL_RU.md" /opt/vpn-bot/INSTALL_RU.md
printf 'telegram-vpn-manager:v%s\n' "$(cat "$ROOT/VERSION")" >/opt/vpn-bot/.vpn-manager-install-id
chmod 0644 /opt/vpn-bot/.vpn-manager-install-id
systemctl daemon-reload
printf '\nПроверяем API (только чтение)...\n'
if /usr/local/sbin/vpn-manager-probe; then
  printf '\nПроверка API прошла. Запустить бота в безопасном режиме (WRITE_ENABLED=false)? [Y/n]: ' >/dev/tty
  IFS= read -r answer </dev/tty
  if [[ "$answer" != n && "$answer" != N ]]; then
    systemctl enable --now vpn-bot
    sleep 3
    if ! systemctl is-active --quiet vpn-bot; then
      echo 'Служба не запустилась. Проверьте: sudo vpn-manager-doctor' >&2
      systemctl status vpn-bot --no-pager -l || true
      exit 1
    fi
    echo 'Бот запущен. Открой Telegram-бота и отправь /start и /admin.'
  else
    echo 'Служба создана, запуск отложен: sudo systemctl enable --now vpn-bot'
  fi
else
  echo 'ВНИМАНИЕ: проверка API не прошла. Бот не запускается. Проверьте URL и токен.' >&2
  echo 'Исправить параметры: sudo vpn-manager-configure; затем sudo vpn-manager-probe.' >&2
fi
cat <<'MSG'

Установка файлов завершена.
  Режим записи: ВЫКЛЮЧЕН (WRITE_ENABLED=false).
  Настройка: sudo vpn-manager-configure
  Проверка: sudo vpn-manager-probe
  Inbound:   sudo vpn-manager-inbounds
  Аудит:     sudo vpn-manager-doctor --api
  Обновить:  sudo bash vpn-manager-setup-<версия>.sh (из нового релиза)
  Бэкап:     sudo vpn-manager-backup --all
  Режим записи: sudo vpn-manager-write-mode
  Удаление:  sudo vpn-manager-uninstall
  Логи:      sudo journalctl -u vpn-bot -n 50 --no-pager
  Инструкция: /opt/vpn-bot/INSTALL_RU.md
3x-ui, nginx, Xray, настройки маршрутизации/DNS не перезапускались установщиком.
MSG
