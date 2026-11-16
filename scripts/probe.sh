#!/usr/bin/env bash
# Read-only check. Never source or execute env file as shell code.
set -Eeuo pipefail
[[ "$EUID" == 0 ]] || { echo 'Запустите через sudo' >&2; exit 1; }
[[ -r /etc/vpn-bot/bot.env ]] || { echo 'Нет /etc/vpn-bot/bot.env' >&2; exit 1; }
cd /opt/vpn-bot
exec /opt/vpn-bot/.venv/bin/python - <<'PY'
import os, shlex, runpy
for line in open('/etc/vpn-bot/bot.env', encoding='utf8'):
    if not line.strip() or line.lstrip().startswith('#'): continue
    k, value = line.rstrip('\n').split('=', 1)
    parts=shlex.split(value, posix=True)
    if len(parts)!=1: raise SystemExit('Ошибка переменной конфигурации: '+k)
    os.environ[k]=parts[0]
runpy.run_module('app.probe', run_name='__main__')
PY
