"""Read-only inbound discovery and validation for the interactive manage command."""
from __future__ import annotations
import asyncio
import os
import shlex
import sys

from app.config import Config, parse_inbound_ids
from app.inbounds import validate_selected_inbounds
from app.xui import XuiAPI


def load_service_env(path: str = '/etc/vpn-bot/bot.env') -> None:
    for line in open(path, encoding='utf-8'):
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, raw = line.rstrip('\n').split('=', 1)
        values = shlex.split(raw, posix=True)
        if len(values) != 1:
            raise ValueError('Некорректная переменная конфигурации: ' + key)
        os.environ[key] = values[0]


async def run(action: str, selection: str = ''):
    load_service_env()
    cfg = Config.from_env()
    api = XuiAPI(cfg.xui_url, cfg.xui_token, cfg.verify_tls, cfg.timeout)
    try:
        inbounds = await api.inbounds()
        if action == 'list':
            print('Существующие inbound в 3x-ui (только чтение):')
            for item in sorted(inbounds, key=lambda i: int(i.get('id', 0))):
                enabled = 'включён' if item.get('enable') is True else 'ВЫКЛЮЧЕН'
                print(f"  ID {item.get('id')} | {str(item.get('protocol') or '?')[:14]} | {enabled} | {str(item.get('remark') or '')[:60]}")
            print('Сейчас выбраны:', ','.join(map(str, cfg.inbound_ids)))
        elif action == 'validate':
            ids = parse_inbound_ids(selection)
            checked = validate_selected_inbounds(ids, inbounds)
            print('Все выбранные inbound существуют и включены:',
                  ','.join(str(x['id']) for x in checked))
        else:
            raise ValueError('Используйте list или validate')
    finally:
        await api.close()


if __name__ == '__main__':
    try:
        if len(sys.argv) < 2 or len(sys.argv) > 3:
            raise ValueError('Использование: python -m app.inbound_cli list|validate [CSV_ID]')
        asyncio.run(run(sys.argv[1], sys.argv[2] if len(sys.argv) == 3 else ''))
    except Exception as exc:
        print(f'Проверка Inbound не пройдена: {exc}', file=sys.stderr)
        raise SystemExit(1)
