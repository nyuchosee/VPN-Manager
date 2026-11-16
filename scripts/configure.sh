#!/usr/bin/env bash
# Interactive, no secrets on argv/history. Run as root, from TTY.
set -Eeuo pipefail
umask 077
[[ "$EUID" -eq 0 ]] || { echo 'Нужен sudo/root.' >&2; exit 1; }
[[ -r /dev/tty ]] || { echo 'Требуется интерактивный терминал (TTY). Не используйте pipe | bash.' >&2; exit 1; }
exec 3</dev/tty
OUTPUT='/etc/vpn-bot/bot.env'
if [[ "${1:-}" == '--output' ]]; then
  [[ $# -eq 2 && "$2" == /* ]] || { echo 'Использование: configure.sh --output /полный/путь' >&2; exit 2; }
  OUTPUT="$2"
elif (( $# )); then echo 'Неизвестный параметр.' >&2; exit 2; fi
ask(){
  local v
  while :; do
    printf '%s' "$2" >/dev/tty
    IFS= read -r -u 3 v || { echo 'Ввод прерван.' >&2; exit 1; }
    [[ -n "$v" ]] || { echo 'Значение обязательно.' >/dev/tty; continue; }
    printf -v "$1" '%s' "$v"; break
  done
}
ask_secret(){
  local v
  while :; do
    printf '%s' "$2" >/dev/tty
    IFS= read -r -s -u 3 v || { echo 'Ввод прерван.' >&2; exit 1; }
    printf '\n' >/dev/tty
    [[ -n "$v" ]] || { echo 'Токен не может быть пустым.' >/dev/tty; continue; }
    printf -v "$1" '%s' "$v"; break
  done
}
check_value(){
  local val="$1" field="$2"
  if [[ "$val" == *$'\n'* || "$val" == *$'\r'* ]]; then
    echo "Недопустимые символы в $field" >&2; exit 1
  fi
}
# Systemd EnvironmentFile double quotes: escape backslash and quote. Never source this config as shell code.
quote_env(){ local s="$1"; s="${s//\\/\\\\}"; s="${s//\"/\\\"}"; printf '"%s"' "$s"; }
cat >/dev/tty <<'MSG'

╔════════════════════════════════════════════════════════╗
║         VPN Manager — мастер конфигурации              ║
╚════════════════════════════════════════════════════════╝
Используй отдельного Telegram-бота, НЕ токен встроенного бота 3x-ui.
Токены при вводе не отображаются.
MSG
ask_secret BOT_TOKEN '1/6  Telegram Bot Token (@BotFather): '
[[ "$BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]] || { echo 'Необычный формат Telegram Bot Token; проверьте значение.' >&2; exit 1; }
ask ADMINS '2/6  Числовые Telegram ID администраторов (через запятую): '
[[ "$ADMINS" =~ ^[0-9]+(,[0-9]+)*$ ]] || { echo 'Используйте только ID, разделённые запятыми, без пробелов.' >&2; exit 1; }
ask PANEL_URL '3/6  HTTPS URL панели 3x-ui, включая секретный base path (без /panel/api): '
[[ "$PANEL_URL" =~ ^https://[^/[:space:]?#]+(/[^[:space:]?#]*)?$ ]] || { echo 'Требуется HTTPS URL без query/fragment/пробелов.' >&2; exit 1; }
PANEL_URL="${PANEL_URL%/}"
ask_secret API_TOKEN '4/6  Отдельный API Token 3x-ui: '
[[ "$API_TOKEN" != *[[:space:]]* ]] || { echo 'API-токен не должен содержать пробелов.' >&2; exit 1; }
cat >/dev/tty <<'MSG'
5/6  ID inbound, куда добавлять НОВЫХ клиентов.
     Например: 1,2 или 1,2,3 (условные ID).
     Указывайте только существующие активные Inbound.
     В дальнейшем их можно изменить через
     sudo vpn-manager-inbounds.
MSG
ask INBOUNDS '     Выбранные ID (через запятую): '
[[ "$INBOUNDS" =~ ^[[:space:]]*[1-9][0-9]*([[:space:]]*,[[:space:]]*[1-9][0-9]*)*[[:space:]]*$ ]] || { echo 'Формат: 1,2 или 1,2,3' >&2; exit 1; }
cat >/dev/tty <<'MSG'
6/6  Шаблон ссылки подписки. Вставь реальный формат URL,
     заменив только существующий subId на {sub_id}, например:
     https://sub.example.org/sub/{sub_id}
     Можно вставить настоящую ссылку, тогда мастер запросит её точный subId.
MSG
ask SUB_TEMPLATE '     URL подписки: '
if [[ "$SUB_TEMPLATE" != *'{sub_id}'* ]]; then
  ask ACTUAL_ID '     Введи точный subId из вставленной ссылки (будет заменён): '
  [[ "$SUB_TEMPLATE" == *"$ACTUAL_ID"* ]] || { echo 'subId не найден в ссылке.' >&2; exit 1; }
  SUB_TEMPLATE="${SUB_TEMPLATE/"$ACTUAL_ID"/\{sub_id\}}"
fi
[[ "$SUB_TEMPLATE" == https://* && "$SUB_TEMPLATE" == *'{sub_id}'* ]] || { echo 'Некорректный шаблон подписки.' >&2; exit 1; }
[[ "$SUB_TEMPLATE" != *[[:space:]]* ]] || { echo 'Пробелы в ссылке недопустимы.' >&2; exit 1; }
for kv in BOT_TOKEN ADMINS PANEL_URL API_TOKEN INBOUNDS SUB_TEMPLATE; do check_value "${!kv}" "$kv"; done
mkdir -p "$(dirname "$OUTPUT")"
TEMP="$(mktemp "$(dirname "$OUTPUT")/.vpn-manager-env.XXXXXXXX")"
trap 'rm -f "$TEMP"' EXIT
{
  printf '# Создан мастером VPN Manager. Не публикуйте.\n'
  printf 'TELEGRAM_BOT_TOKEN='; quote_env "$BOT_TOKEN"; printf '\n'
  printf 'ADMIN_IDS='; quote_env "$ADMINS"; printf '\n'
  printf 'XUI_URL='; quote_env "$PANEL_URL"; printf '\n'
  printf 'XUI_API_TOKEN='; quote_env "$API_TOKEN"; printf '\n'
  printf 'INBOUND_IDS='; quote_env "$INBOUNDS"; printf '\n'
  printf 'SUBSCRIPTION_URL_TEMPLATE='; quote_env "$SUB_TEMPLATE"; printf '\n'
  printf 'WRITE_ENABLED=false\n'
  printf 'INSECURE_SKIP_TLS_VERIFY=false\n'
  printf 'DATABASE_PATH=/var/lib/vpn-bot/bot.sqlite3\n'
  printf 'HTTP_TIMEOUT=15\n'
} >"$TEMP"
chmod 0600 "$TEMP"
# Validate configuration without executing or eval'ing contents.
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ ! -f "$BASE/app/config.py" && -f /opt/vpn-bot/app/config.py ]]; then BASE=/opt/vpn-bot; fi
VPN_MANAGER_ENV_CHECK="$TEMP" PYTHONPATH="$BASE" python3 - <<'PY'
import os, shlex
from app.config import Config
path=os.environ.pop('VPN_MANAGER_ENV_CHECK')
for line in open(path, encoding='utf-8'):
    if not line.strip() or line.lstrip().startswith('#'): continue
    k, raw=line.rstrip('\n').split('=',1)
    parts=shlex.split(raw, posix=True)
    if len(parts)!=1: raise SystemExit('Некорректный формат переменной '+k)
    os.environ[k]=parts[0]
c=Config.from_env()
assert c.write_enabled is False and c.verify_tls is True
print('Параметры прошли локальную проверку. Режим записи отключён.')
PY
if [[ "$OUTPUT" == '/etc/vpn-bot/bot.env' && -f "$OUTPUT" ]]; then
  echo 'Перед заменой существующего bot.env будет создан бэкап.' >/dev/tty
  if command -v vpn-manager-backup >/dev/null; then vpn-manager-backup --bot; else cp -p "$OUTPUT" "${OUTPUT}.previous.$(date -u +%s)"; fi
fi
if [[ "$OUTPUT" == '/etc/vpn-bot/bot.env' ]]; then
  chown root:vpn-bot "$TEMP"
  chmod 0640 "$TEMP"
fi
mv -f "$TEMP" "$OUTPUT"
trap - EXIT
printf '\nКонфигурация сохранена: %s\n' "$OUTPUT"
echo 'WRITE_ENABLED=false: реальное изменение клиентов отключено.'
echo 'Перенастройка INBOUND_IDS влияет на НОВЫХ клиентов; существующие привязки автоматически не меняются.'
if [[ "$OUTPUT" == '/etc/vpn-bot/bot.env' ]]; then
  echo 'Чтобы применить новые настройки: sudo systemctl restart vpn-bot'
fi
