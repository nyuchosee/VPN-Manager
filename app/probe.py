"""Read-only health check; never prints secrets, UUID, subId or user identities."""
import asyncio
from app.config import Config
from app.xui import XuiAPI, update_payload, XuiError
from app.inbounds import validate_selected_inbounds

async def check():
    c=Config.from_env()
    api=XuiAPI(c.xui_url,c.xui_token,c.verify_tls,c.timeout)
    try:
        clients=await api.clients()
        print('API доступно; количество клиентов:',len(clients))
        print('Режим записи бота:', 'включён' if c.write_enabled else 'выключен')
        print('Выбранные inbound ID:',list(c.inbound_ids))
        inbounds=await api.inbounds()
        print('Найдено inbound в панели:', len(inbounds))
        selected=validate_selected_inbounds(c.inbound_ids, inbounds)
        for row in selected:
            print('  ID', row['id'], '|', row.get('protocol', '?'), '| включён |', str(row.get('remark') or '')[:50])
        print('Проверка всех выбранных inbound: OK')
        failures=0
        for index,row in enumerate(clients,1):
            email=row.get('email')
            if not email:
                failures+=1
                print('  [FAIL] У клиента №'+str(index)+' отсутствует идентификатор email')
                continue
            try:
                client=await api.get(email)
                if client.get('email')!=email:
                    raise XuiError('Запись API не совпадает с запрошенным клиентом')
                update_payload(client,{})   # DRY RUN, не отправляет POST!
            except XuiError as exc:
                failures+=1
                print('  [FAIL] Запись №'+str(index)+': '+str(exc))
        print('Проверено форматов клиентов:',len(clients),'; обнаружено проблем:',failures)
        if failures:
            raise XuiError('Не все клиенты совместимы с форматом обновления; запись запрещена')
        if clients:
            first=await api.get(clients[0]['email'])
            print('Наличие subId у первого клиента:', bool(first.get('subId')))
            print('Число привязанных inbound:',len(first.get('inboundIds') or []))
            try:
                devices=await api.hwids(first['email'])
                print('Чтение HWID: OK, число записей',len(devices))
            except XuiError as exc:
                print('HWID API: проверить вручную:',str(exc)[:140])
        print('ВНИМАНИЕ: API не получало запросов изменения. Данные только прочитаны.')
    finally:
        await api.close()

if __name__=='__main__':
    asyncio.run(check())
