#!/usr/bin/env bash
# Explicit approval gate for enabling API writes. Backups are mandatory.
set -Eeuo pipefail
umask 077
[[ "$EUID" == 0 ]] || { echo 'Нужен sudo/root.' >&2; exit 1; }
[[ -r /dev/tty ]] || { echo 'Необходим интерактивный терминал SSH.' >&2; exit 1; }
ENV_FILE=/etc/vpn-bot/bot.env
[[ -f "$ENV_FILE" ]] || { echo 'Конфигурация VPN Manager не найдена.' >&2; exit 1; }
if [[ $# -eq 0 ]]; then
  printf 'Выберите режим: [1] Только чтение (безопасно), [2] Разрешить изменение клиентов: ' >/dev/tty
  IFS= read -r selected </dev/tty
  case "$selected" in 1) MODE=disable;; 2) MODE=enable;; *) echo 'Отменено.'; exit 0;; esac
elif [[ $# -eq 1 && "$1" == '--enable' ]]; then MODE=enable
elif [[ $# -eq 1 && "$1" == '--disable' ]]; then MODE=disable
else
  echo 'Использование: sudo vpn-manager-write-mode [--enable|--disable]' >&2; exit 2
fi
if [[ "$MODE" == enable ]]; then
  cat >/dev/tty <<'MSG'

ВНИМАНИЕ: бот получит право создавать и изменять VPN-клиентов!
Перед этим:
 - проверьте совместимость API с текущей 3x-ui;
 - сделайте тест на отдельном клиенте;
 - убедитесь, что есть внешняя проверенная резервная копия.
Сейчас будут выполнены read-only проверка API и новый локальный бэкап.
MSG
  /usr/local/sbin/vpn-manager-probe
  /usr/local/sbin/vpn-manager-backup --all
  printf 'Чтобы РАЗРЕШИТЬ операции записи, введите точную фразу РАЗРЕШАЮ-ЗАПИСЬ: ' >/dev/tty
  IFS= read -r phrase </dev/tty
  [[ "$phrase" == 'РАЗРЕШАЮ-ЗАПИСЬ' ]] || { echo 'Отменено: режим записи не включён.'; exit 0; }
fi
TEMP="$(mktemp /etc/vpn-bot/.write-mode.XXXXXXXX)"
trap 'rm -f "$TEMP"' EXIT
python3 - "$ENV_FILE" "$TEMP" "$MODE" <<'PY'
import pathlib, sys
src,dst,mode=sys.argv[1:]
lines=pathlib.Path(src).read_text(encoding='utf8').splitlines(keepends=True)
found=False
for i,l in enumerate(lines):
    if l.startswith('WRITE_ENABLED='):
        if found: raise SystemExit('Дублируется WRITE_ENABLED; проверьте файл вручную.')
        lines[i]='WRITE_ENABLED='+('true' if mode=='enable' else 'false')+'\n'
        found=True
if not found: raise SystemExit('Не найдена переменная WRITE_ENABLED')
pathlib.Path(dst).write_text(''.join(lines),encoding='utf8')
PY
chown root:vpn-bot "$TEMP"
chmod 0640 "$TEMP"
OLD="$(mktemp /etc/vpn-bot/.previous-mode.XXXXXXXX)"
trap 'rm -f "$TEMP" "$OLD"' EXIT
cp -p "$ENV_FILE" "$OLD"
active=0
if systemctl is-active --quiet vpn-bot; then active=1; systemctl stop vpn-bot; fi
mv -f "$TEMP" "$ENV_FILE"
if (( active )); then
  if ! systemctl start vpn-bot; then
    echo 'Не удалось запустить бот с новыми настройками, возвращаем прежний режим.' >&2
    cp -p "$OLD" "$ENV_FILE"
    systemctl start vpn-bot || echo 'Не удалось восстановить запуск vpn-bot, требуется ручная проверка.' >&2
    exit 1
  fi
fi
rm -f "$OLD"
trap - EXIT
if [[ "$MODE" == enable ]]; then
  echo 'Режим записи ВКЛЮЧЁН. Сначала тестируйте на специально созданном клиенте!'
else
  echo 'Режим записи ОТКЛЮЧЁН. Запросы к 3x-ui теперь только на чтение.'
fi
