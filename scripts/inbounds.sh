#!/usr/bin/env bash
# Manage selected inbound IDs without re-entering Telegram or 3x-ui API secrets.
# Does not attach previously-created clients to newly selected inbounds.
set -Eeuo pipefail
umask 077
[[ "$EUID" == 0 ]] || { echo 'Запустите через sudo/root.' >&2; exit 1; }
[[ -r /dev/tty ]] || { echo 'Нужен SSH TTY.' >&2; exit 1; }
ENV_FILE=/etc/vpn-bot/bot.env
PYTHON=/opt/vpn-bot/.venv/bin/python
[[ -f "$ENV_FILE" && -x "$PYTHON" ]] || { echo 'VPN Manager не установлен.' >&2; exit 1; }
cd /opt/vpn-bot
printf '\nVPN Manager — настройка нескольких Inbound\n'
"$PYTHON" -m app.inbound_cli list || exit 1
cat >/dev/tty <<'MSG'

Введите полный перечень активных Inbound ID для НОВЫХ подписок.
Пример: 1,2,4; после создания ещё двух: 1,2,4,5,6.
Ранее созданные клиенты не перепривязываются автоматически.
MSG
printf 'Новый список ID (через запятую): ' >/dev/tty
IFS= read -r IDS </dev/tty
"$PYTHON" -m app.inbound_cli validate "$IDS" || exit 1
printf 'Подтвердите изменение списка и отключение режима записи [y/N]: ' >/dev/tty
IFS= read -r answer </dev/tty
[[ "$answer" == y || "$answer" == Y ]] || { echo 'Отменено.'; exit 0; }
/usr/local/sbin/vpn-manager-backup --bot
TEMP="$(mktemp /etc/vpn-bot/.inbounds.XXXXXXXX)"
OLD="$(mktemp /etc/vpn-bot/.inbounds-old.XXXXXXXX)"
trap 'rm -f "$TEMP" "$OLD"' EXIT
cp -p "$ENV_FILE" "$OLD"
python3 - "$ENV_FILE" "$TEMP" "$IDS" <<'PY'
from pathlib import Path
import sys
from app.config import parse_inbound_ids
src,dst,raw=sys.argv[1:]
ids=parse_inbound_ids(raw)
lines=Path(src).read_text(encoding='utf-8').splitlines(keepends=True)
found={'INBOUND_IDS':0,'WRITE_ENABLED':0}
for i,line in enumerate(lines):
    for key in tuple(found):
        if line.startswith(key+'='):
            found[key] += 1
            if found[key]>1: raise SystemExit('Дублируется '+key)
            lines[i]= key+'='+('"'+','.join(map(str,ids))+'"' if key=='INBOUND_IDS' else 'false')+'\n'
if any(n!=1 for n in found.values()):raise SystemExit('Не найдены INBOUND_IDS или WRITE_ENABLED')
Path(dst).write_text(''.join(lines),encoding='utf-8')
PY
chown root:vpn-bot "$TEMP"
chmod 0640 "$TEMP"
active=0
if systemctl is-active --quiet vpn-bot; then active=1; systemctl stop vpn-bot; fi
mv -f "$TEMP" "$ENV_FILE"
if ((active)); then
    if ! systemctl start vpn-bot; then
        echo 'Не удалось запустить bot. Возвращаем старую конфигурацию.' >&2
        cp -p "$OLD" "$ENV_FILE"
        systemctl start vpn-bot || echo 'Не удалось перезапустить vpn-bot; проверьте systemctl status vpn-bot.' >&2
        exit 1
    fi
fi
trap - EXIT
rm -f "$OLD"
echo 'Новые ID сохранены. Режим записи выключен. Проверьте sudo vpn-manager-probe.'
echo 'Для добавления выбранных inbound к существующим клиентам используйте ручное административное действие в 3x-ui.'
