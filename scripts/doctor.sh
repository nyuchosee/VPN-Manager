#!/usr/bin/env bash
# Diagnostic only. No writes to 3x-ui, no printing of bot.env or API credentials.
set -Eeuo pipefail
umask 077
[[ $EUID -eq 0 ]] || { echo 'Запустите: sudo vpn-manager-doctor [--api]' >&2; exit 1; }
[[ $# -eq 0 || ( $# -eq 1 && "$1" == --api ) ]] || { echo 'Использование: vpn-manager-doctor [--api]' >&2; exit 2; }
FAILED=0
check(){ if "$@" >/dev/null 2>&1; then printf '  [OK] %s\n' "$*"; else printf '  [FAIL] %s\n' "$*"; FAILED=1; fi; }
echo 'VPN Manager — безопасная диагностика v0.5.2'
echo 'Проверки файлов, прав запуска и службы. Секреты не выводятся.'
[[ -f /etc/vpn-bot/bot.env ]] && echo '  [OK] bot.env существует' || { echo '  [FAIL] bot.env отсутствует'; FAILED=1; }
if grep -q '^WRITE_ENABLED=false$' /etc/vpn-bot/bot.env 2>/dev/null; then
  echo '  [OK] Запись в 3x-ui отключена'
else
  echo '  [WARN] Запись может быть включена; проверьте отдельно режим через vpn-manager-write-mode'
fi
if id vpn-bot >/dev/null 2>&1; then
  check runuser -u vpn-bot -- test -r /etc/vpn-bot/bot.env
  check runuser -u vpn-bot -- test -r /opt/vpn-bot/app/main.py
  check runuser -u vpn-bot -- test -x /opt/vpn-bot/.venv/bin/python
  check runuser -u vpn-bot -- /opt/vpn-bot/.venv/bin/python --version
  if [[ -x /opt/vpn-bot/.venv/bin/python ]]; then
    check runuser -u vpn-bot -- sh -c 'cd /opt/vpn-bot && /opt/vpn-bot/.venv/bin/python -c "import aiogram,httpx; import app.main"'
  fi
else
  echo '  [FAIL] Нет системного пользователя vpn-bot'; FAILED=1
fi
if [[ -f /var/lib/vpn-bot/bot.sqlite3 ]]; then
  echo '  [OK] Файл SQLite присутствует'
  python3 - <<'PY' || FAILED=1
import sqlite3
try:
    db=sqlite3.connect('file:/var/lib/vpn-bot/bot.sqlite3?mode=ro',uri=True)
    print('  [OK] SQLite quick_check:',db.execute('PRAGMA quick_check').fetchone()[0])
    columns={r[1] for r in db.execute('PRAGMA table_info(operations)')}
    if 'last_progress_stage' in columns:
        query="SELECT request_id,stage,COALESCE(last_progress_stage,'') FROM operations WHERE stage='needs_review' ORDER BY id DESC LIMIT 15"
    else:
        query="SELECT request_id,stage,'' FROM operations WHERE stage='needs_review' ORDER BY id DESC LIMIT 15"
    for rid,stage,prev in db.execute(query):
        print(f'  [REVIEW] Заявка №{rid}; последний этап: {prev or "неизвестен"}; повторять нельзя')
    db.close()
except Exception as exc:
    print('  [FAIL] Проверка SQLite:',type(exc).__name__)
    raise SystemExit(1)
PY
fi
printf '  [INFO] Служба vpn-bot: %s\n' "$(systemctl is-active vpn-bot 2>/dev/null || true)"
if command -v findmnt >/dev/null; then
  options=$(findmnt -no OPTIONS -T /opt/vpn-bot/.venv/bin/python 2>/dev/null || true)
  if [[ ",${options}," == *,noexec,* ]]; then echo '  [FAIL] файловая система noexec'; FAILED=1; fi
fi
if [[ "${1:-}" == --api ]]; then
  echo 'Read-only диагностика API (с безопасным выводом):'
  /usr/local/sbin/vpn-manager-probe || FAILED=1
fi
if [[ $FAILED -eq 0 ]]; then echo 'Локальная диагностика завершена. Отсутствие ошибок здесь НЕ означает успешную запись в 3x-ui.'; else
  echo 'Обнаружены проблемы. Для локализации: namei -l /opt/vpn-bot/.venv/bin/python'
fi
exit "$FAILED"
