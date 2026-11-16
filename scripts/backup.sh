#!/usr/bin/env bash
# Scoped backup for Telegram VPN Manager. NEVER stops or modifies Xray/nginx/x-ui.
set -Eeuo pipefail
umask 077
if (( EUID != 0 )); then echo 'Запустите с sudo/root.' >&2; exit 1; fi
MODE="${1:---all}"
case "$MODE" in --panel|--bot|--all) ;; *) echo 'Использование: vpn-manager-backup [--panel|--bot|--all]' >&2; exit 2;; esac
command -v python3 >/dev/null || { echo 'Требуется python3.' >&2; exit 1; }
command -v tar >/dev/null || { echo 'Требуется tar.' >&2; exit 1; }
BACKUP_ROOT="${VPN_MANAGER_BACKUP_ROOT:-/var/backups/vpn-manager}"
[[ "$BACKUP_ROOT" == /* && "$BACKUP_ROOT" != / ]] || { echo 'Недопустимый каталог бэкапов.' >&2; exit 1; }
install -d -m 0700 "$BACKUP_ROOT"
OUT="$(mktemp -d "$BACKUP_ROOT/$(date -u +%Y%m%dT%H%M%SZ)-XXXXXXXX")"
chmod 0700 "$OUT"
cleanup(){ if [[ "${OK:-0}" != 1 ]]; then echo "ОШИБКА: неполная копия находится в $OUT (не используйте её для восстановления)" >&2; fi; }
trap cleanup EXIT
backup_sqlite(){
  local source="$1" destination="$2"
  python3 - "$source" "$destination" <<'PY'
import pathlib, sqlite3, sys
src, dst = map(pathlib.Path, sys.argv[1:])
if not src.is_file(): raise SystemExit(f'База не найдена: {src}')
# Read-only source: SQLite online backup (no x-ui stop, no raw db+WAL copy).
a = sqlite3.connect(src.resolve().as_uri()+'?mode=ro', uri=True, timeout=15)
b = sqlite3.connect(str(dst))
try:
    a.backup(b, pages=256, sleep=0.1)
    result = b.execute('PRAGMA integrity_check').fetchone()[0]
    if result != 'ok': raise RuntimeError('Нарушена целостность копии: '+result)
finally:
    b.close(); a.close()
dst.chmod(0o600)
print('SQLite: копия создана и проверена (integrity_check=ok)')
PY
}
archive_paths(){
  local output="$1"; shift
  local arr=() p
  for p in "$@"; do [[ -e "/$p" ]] && arr+=("$p"); done
  if ((${#arr[@]})); then
    tar -czf "$output" -C / --exclude='etc/x-ui/x-ui.db' --exclude='etc/x-ui/x-ui.db-shm' --exclude='etc/x-ui/x-ui.db-wal' --exclude='var/lib/vpn-bot/bot.sqlite3' --exclude='var/lib/vpn-bot/bot.sqlite3-shm' --exclude='var/lib/vpn-bot/bot.sqlite3-wal' --exclude='opt/vpn-bot/.venv' "${arr[@]}"
    tar -tzf "$output" >/dev/null
    chmod 0600 "$output"
    echo 'Конфигурационные файлы архивированы.'
  fi
}
if [[ "$MODE" != '--bot' ]]; then
  DB="${XUI_DB_PATH:-/etc/x-ui/x-ui.db}"
  [[ "$DB" == /* ]] || { echo 'XUI_DB_PATH должен быть абсолютным путём' >&2; exit 1; }
  backup_sqlite "$DB" "$OUT/x-ui.db"
  # Sensitive nginx/Xray/routing (including DNS) settings and referenced TLS files.
  archive_paths "$OUT/panel-config.tar.gz" etc/x-ui etc/nginx etc/letsencrypt root/cert etc/systemd/system/x-ui.service usr/local/etc/xray
fi
if [[ "$MODE" != '--panel' ]]; then
  if [[ -f /var/lib/vpn-bot/bot.sqlite3 ]]; then
    backup_sqlite /var/lib/vpn-bot/bot.sqlite3 "$OUT/vpn-bot.sqlite3"
  fi
  archive_paths "$OUT/bot-config.tar.gz" etc/vpn-bot opt/vpn-bot/app etc/systemd/system/vpn-bot.service
fi
(
  cd "$OUT"
  files=(); for f in *.db *.tar.gz; do [[ -f "$f" ]] && files+=("$f"); done
  if ((${#files[@]})); then sha256sum "${files[@]}" > SHA256SUMS; else echo 'Не найдены файлы резервной копии' >&2; exit 1; fi
  sha256sum -c SHA256SUMS >/dev/null
)
OK=1
printf '\nРезервная копия готова: %s\n' "$OUT"
echo 'СОДЕРЖИТ СЕКРЕТЫ. Сохраните копию за пределами VPS в защищённом месте.'
echo 'Восстановление не выполнялось. Для полного disaster-recovery используйте также x-ui-backup в окно обслуживания.'
