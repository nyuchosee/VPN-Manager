#!/usr/bin/env python3
"""Build self-contained SHA256-verifying Bash setup for fresh/upgrade installs."""
from __future__ import annotations
import base64
import hashlib
import io
from pathlib import Path
import stat
import zipfile

root=Path(__file__).resolve().parent.parent
version=(root/'VERSION').read_text().strip()
dist=root/'dist'
dist.mkdir(exist_ok=True)
name=f'vpn-manager-setup-{version}.sh'
excluded={'.git','dist','.venv','venv','__pycache__','.pytest_cache','.idea','.mypy_cache'}
files=sorted(p for p in root.rglob('*') if p.is_file()
  and not any(bit in excluded for bit in p.relative_to(root).parts)
  and p.suffix not in ('.zip','.sqlite3','.db','.pyc'))
buffer=io.BytesIO()
with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
  for path in files:
    entry='telegram-vpn-manager/'+path.relative_to(root).as_posix()
    info=zipfile.ZipInfo(entry,date_time=(2020,1,1,0,0,0))
    info.create_system=3
    info.external_attr=(stat.S_IFREG | (0o755 if path.suffix in ('.sh','.py') else 0o644))<<16
    info.compress_type=zipfile.ZIP_DEFLATED
    archive.writestr(info,path.read_bytes(),compress_type=zipfile.ZIP_DEFLATED,compresslevel=9)
blob=buffer.getvalue()
digest=hashlib.sha256(blob).hexdigest()
header=f'''#!/usr/bin/env bash
# VPN Manager {version}: SHA256-verified offline archive + online dependency installer.
set -Eeuo pipefail
umask 077
cat <<'BANNER'
 __     ______  _   _    __  __    _    _   _    _    ____ _____ ____
 \\ \\   / /  _ \\| \\ | |  |  \\/  |  / \\  | \\ | |  / \\  / ___| ____|  _ \\
  \\ \\ / /| |_) |  \\| |  | |\\/| | / _ \\ |  \\| | / _ \\| |  _|  _| | |_) |
   \\ V / |  __/| |\\  |  | |  | |/ ___ \\| |\\  |/ ___ \\ |_| | |___|  _ <
    \\_/  |_|   |_| \\_|  |_|  |_/_/   \\_\\_| \\_/_/   \\_\\____|_____|_| \\_\\

                     TELEGRAM VPN MANAGER {version}
BANNER
export VPN_MANAGER_BANNER_SHOWN=1
command -v python3 >/dev/null || {{ echo 'Требуется Python 3.10+.' >&2; exit 1; }}
WORK="$(mktemp -d /tmp/vpn-manager-release.XXXXXXXX)"
trap 'rm -rf "$WORK"' EXIT
python3 - "$0" "$WORK" '{digest}' <<'PYTHON_UNPACK'
import base64,hashlib,io,pathlib,sys,zipfile
path,target,expected=sys.argv[1:]
raw=pathlib.Path(path).read_bytes()
marker=b'\\n__VPN_MANAGER_PAYLOAD_BELOW__\\n'
if marker not in raw:
    raise SystemExit('Отсутствует встроенный архив.')
encoded=b''.join(raw.rsplit(marker,1)[1].split())
blob=base64.b64decode(encoded,validate=True)
if hashlib.sha256(blob).hexdigest()!=expected:
    raise SystemExit('SHA-256 встроенного архива не совпадает!')
with zipfile.ZipFile(io.BytesIO(blob)) as z:
    for item in z.infolist():
        p=pathlib.PurePosixPath(item.filename)
        if p.is_absolute() or '..' in p.parts or not p.parts or p.parts[0]!='telegram-vpn-manager':
            raise SystemExit('Небезопасный путь в ZIP: '+item.filename)
        mode=(item.external_attr>>16)&0o170000
        if mode==0o120000:
            raise SystemExit('Символические ссылки в ZIP запрещены')
    z.extractall(target)
print('Проверено файлов:',len(z.namelist()))
print('Контрольная сумма архива: OK')
PYTHON_UNPACK
if [[ "${{1:-}}" == '--verify' ]]; then
  echo 'Режим проверки: файлы системы не изменялись.'
  exit 0
fi
[[ $EUID -eq 0 ]] || {{ echo 'Запустите: sudo bash {name}' >&2; exit 1; }}
exec bash "$WORK/telegram-vpn-manager/scripts/install.sh"
'''
# The exec replaces the current shell and would lose the EXIT trap cleanup;
# avoid exec so the temporary files are removed on normal completion.
header=header.replace('exec bash "$WORK/telegram-vpn-manager/scripts/install.sh"', 'bash "$WORK/telegram-vpn-manager/scripts/install.sh"')
encoded=base64.b64encode(blob).decode('ascii')
lines='\n'.join(encoded[i:i+76] for i in range(0,len(encoded),76))
output=dist/name
output.write_bytes(header.encode('utf8')+b'\n__VPN_MANAGER_PAYLOAD_BELOW__\n'+lines.encode('ascii')+b'\n')
output.chmod(0o755)
full_digest=hashlib.sha256(output.read_bytes()).hexdigest()
(dist/'SHA256SUMS').write_text(full_digest+'  '+name+'\n')
print('Сборка:',output,'файлов:',len(files),'SHA256:',full_digest)
