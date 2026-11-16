"""Business rules; explicit, journaled, fail-closed mutations."""
from __future__ import annotations
import asyncio
from copy import deepcopy
from typing import Any
from app.domain import now_ms, replace_expiry, exact_expiry, gb_bytes, validate_hwid, client_name, status
from app.storage import Store
from app.xui import XuiError
from app.inbounds import validate_selected_inbounds
from app.security import validate_alias


class Service:
    def __init__(self, store: Store, api, inbound_ids: tuple[int,...], write_enabled:bool):
        self.store=store
        self.api=api
        self.inbound_ids=inbound_ids
        self.write_enabled=write_enabled
        self._lock=asyncio.Lock()

    def _can_write(self):
        if not self.write_enabled:
            raise ValueError('Режим только чтения. Для испытаний запись включается в bot.env: WRITE_ENABLED=true')

    async def client_for(self,tg_id:int):
        binding=self.store.binding(tg_id)
        if not binding:
            return None
        return await self.api.get(binding['email'])

    async def link_for(self,tg_id:int,template:str):
        c=await self.client_for(tg_id)
        if not c or not c.get('subId'):
            return None
        return template.replace('{sub_id}', str(c['subId']))

    async def register_existing(self,tg_id:int,email:str,admin:int):
        # Never infer proof of ownership from a username or a URL alone.
        async with self._lock:
            if self.store.binding(tg_id) or self.store.binding_email(email):
                raise ValueError('Эта связь уже зарегистрирована')
            c=await self.api.get(email)
            if c.get('email')!=email:
                raise ValueError('Имя клиента не совпадает с записью панели')
            existing_tg=int(c.get('tgId') or 0)
            if existing_tg and existing_tg!=tg_id:
                raise ValueError('В 3x-ui указан другой Telegram ID. Разберитесь с принадлежностью клиента до привязки')
            self.store.bind(tg_id,email)
            self.store.audit(admin,tg_id,'bind',f'Привязка клиента {email} без изменений в 3x-ui')

    def create_user_request(self,tg_id:int,days:int):
        if days not in (7,30,90): raise ValueError('Допустимый выбор: 7, 30, 90 дней')
        if self.store.binding(tg_id): raise ValueError('Первичная выдача доступна только один раз; используйте продление')
        return self.store.create_request(tg_id,'create',days=days)

    def renew_request(self,tg_id:int,days:int):
        if days not in (7,30,90): raise ValueError('Допустимый выбор: 7, 30, 90 дней')
        if not self.store.binding(tg_id): raise ValueError('Сначала необходимо получить доступ')
        return self.store.create_request(tg_id,'renew',days=days)

    def device_request(self,tg_id:int,device_id:int):
        if not self.store.binding(tg_id): raise ValueError('Подписка не найдена')
        if device_id<=0: raise ValueError('Неверный идентификатор устройства')
        return self.store.create_request(tg_id,'hwid_delete',device_id=device_id)

    def slots_request(self,tg_id:int,slots:int):
        if not self.store.binding(tg_id): raise ValueError('Подписка не найдена')
        if not 1<=slots<=100: raise ValueError('Допустимое увеличение: 1–100 мест')
        return self.store.create_request(tg_id,'hwid_slots',slots=slots)

    async def reject(self,request_id:int,admin:int):
        async with self._lock:
            req=self.store.request(request_id)
            if not req or not self.store.transition(request_id,'pending','rejected','Отклонена администратором',admin):
                raise ValueError('Заявка уже обработана')
            self.store.audit(admin,req['tg_id'],'reject',f'Заявка #{request_id} отклонена')
            return req

    def _settings(self,req):
        cfg=self.store.settings(req)
        if 'alias' in cfg:
            if req['kind']!='create':
                raise ValueError('Псевдоним при выдаче разрешён только для первичной заявки')
            cfg['alias']=validate_alias(cfg['alias'])
        kind=req['kind']
        days=cfg.get('days',req['requested_days'])
        if kind in ('create','renew') and days is not None:
            if not isinstance(days,int) or not 1<=days<=36500:
                raise ValueError('Некорректный срок')
        gb=cfg.get('traffic_gb',500)
        if kind in ('create','renew'):
            gb_bytes(gb)
        n=cfg.get('hwid_limit',3 if kind=='create' else None)
        if n is not None:
            validate_hwid(n)
        if 'expiry_at' in cfg:
            when=cfg['expiry_at']
            if not isinstance(when,int) or when <= now_ms() or when > now_ms()+36500*86_400_000:
                raise ValueError('Дата окончания должна быть в будущем')
        return cfg,days,gb,n

    async def approve(self,request_id:int,admin:int):
        self._can_write()
        async with self._lock:
            req=self.store.request(request_id)
            if not req or req['status']!='pending':
                raise ValueError('Заявка уже обработана или требует ручной проверки')
            cfg,days,gb,hwid=self._settings(req)
            binding=self.store.binding(req['tg_id'])
            if req['kind']=='create' and binding:
                raise ValueError('Клиент уже существует; использовать продление')
            if req['kind']!='create' and not binding:
                raise ValueError('Нет привязки клиента')
            if req['kind']=='create':
                if not self.inbound_ids: raise ValueError('Не задано INBOUND_IDS в конфигурации')
                # A single 3x-ui client will be attached to every selected inbound.
                # Fail before any mutation if the operator configured an inbound
                # that has not been created yet or is currently disabled.
                validate_selected_inbounds(self.inbound_ids, await self.api.inbounds())
                existing=await self.api.clients()
                user=self.store.user(req['tg_id']) or {}
                taken={str(c.get('email','')) for c in existing}
                # Avoid overwriting unbound prior accounts associated with same Telegram ID.
                if any(int(c.get('tgId') or 0)==req['tg_id'] for c in existing):
                    raise ValueError('В 3x-ui уже есть клиент с этим Telegram ID. Привяжите его вручную')
                email=client_name(user.get('username'),req['tg_id'],taken,cfg.get('alias'))
                plan={'email':email,'days':days,'gb':gb,'hwid':hwid,'tg_id':req['tg_id'],
                      'timestamp':now_ms(), 'inbounds':list(self.inbound_ids), 'expiry_at':cfg.get('expiry_at'),
                      'display_name':cfg.get('alias') or ''}
            else:
                email=binding['email']
                c=await self.api.get(email)
                if not c or c.get('email')!=email: raise ValueError('Клиент не найден')
                plan={'email':email,'kind':req['kind'],'timestamp':now_ms(),
                      'pre_sub':c.get('subId'),'pre_ids':c.get('inboundIds'),
                      'pre_public':{k:c.get(k) for k in ('expiryTime','totalGB','limitHwid','enable','tgId')}}
                if req['kind']=='delete':
                    if not cfg.get('delete_confirm'):
                        raise ValueError('Удаление требует отдельного подтверждения')
                    if self.store.pending_others(req['tg_id'],request_id):
                        raise ValueError('Есть другие открытые заявки клиента. Сначала разрешите их')
                if req['kind']=='renew':
                    if c.get('enable') is False and not binding['manual_block'] and not cfg.get('reactivate'):
                        raise ValueError('Клиент отключён. Администратор должен явно разрешить восстановление при продлении')
                    if int(c.get('expiryTime') or 0)==0 and (days is not None or 'expiry_at' in cfg) and not cfg.get('explicit_from_unlimited',False):
                        raise ValueError('Подписка бессрочная. Для перевода на ограниченный срок необходимо явное подтверждение')
                    plan.update({'reactivate': bool(cfg.get('reactivate')), 'expiry':cfg['expiry_at'] if 'expiry_at' in cfg else replace_expiry(days),
                                 'gb':gb_bytes(gb),'hwid':hwid, 'hwid_explicit': 'hwid_limit' in cfg,
                                 'was_enabled':bool(c.get('enable',True))})
                elif req['kind']=='adjust':
                    changes={}
                    if 'expiry_at' in cfg:
                        if int(c.get('expiryTime') or 0)==0 and not cfg.get('explicit_from_unlimited'):
                            raise ValueError('Подтвердите перевод бессрочной подписки в срочную')
                        changes['expiryTime']=cfg['expiry_at']
                    elif 'days' in cfg:
                        if int(c.get('expiryTime') or 0) == 0 and cfg['days'] is not None and not cfg.get('explicit_from_unlimited'):
                            raise ValueError('Нужно явно подтвердить перевод бессрочной подписки в срочную')
                        changes['expiryTime']=replace_expiry(cfg['days'])
                    if 'traffic_gb' in cfg: changes['totalGB']=gb_bytes(cfg['traffic_gb'])
                    if 'hwid_limit' in cfg: changes['limitHwid']=validate_hwid(cfg['hwid_limit'])
                    if not changes: raise ValueError('Укажите хотя бы один параметр')
                    plan['changes']=changes
                elif req['kind'] in ('block','unblock'):
                    if req['kind']=='unblock' and (int(c.get('expiryTime') or 0)<0 or (int(c.get('expiryTime') or 0)>0 and int(c.get('expiryTime'))<=now_ms())):
                        raise ValueError('Нельзя разблокировать истёкший аккаунт без продления')
                    plan['changes']={'enable': req['kind']=='unblock'}
                elif req['kind']=='hwid_slots':
                    current=int(c.get('limitHwid') or 0)
                    requested=req['requested_slots']
                    if 'hwid_limit' in cfg:
                        target=validate_hwid(cfg['hwid_limit'])
                    else:
                        if current==0: raise ValueError('HWID уже без ограничения')
                        target=validate_hwid(current+requested)
                    plan['hwid']=target
                elif req['kind']=='delete':
                    plan['delete_confirm']=True
                elif req['kind']=='hwid_delete':
                    devices=await self.api.hwids(email)
                    if not any(int(d.get('id',0))==req['device_id'] for d in devices):
                        raise ValueError('Устройство уже отсутствует; обновите заявку')
                    plan['device_id']=req['device_id']
            if req['kind'] in ('adjust','hwid_slots','renew'):
                target=(plan.get('changes') or {}).get('limitHwid') if req['kind']=='adjust' else (plan.get('hwid') if req['kind']=='hwid_slots' or plan.get('hwid_explicit') else None)
                if target is not None and target>0:
                    devices=await self.api.hwids(email)
                    if len(devices)>target:
                        raise ValueError('Новый лимит ниже количества зарегистрированных HWID. Сначала удалите лишние устройства')
            # Journal first; if process crashes during API call, we never blindly replay.
            if not self.store.transition(request_id,'pending','processing','Выполняется',admin):
                raise ValueError('Заявка уже обработана')
            self.store.add_operation(request_id,req['kind'],email,plan)
            try:
                result=await self._execute(req,plan)
            except Exception as exc:
                self.store.operation_stage(request_id,'needs_review')
                self.store.transition(request_id,'processing','needs_review',str(exc)[:250],admin)
                self.store.audit(admin,req['tg_id'],'needs_review',f'Заявка #{request_id}: {type(exc).__name__}')
                raise ValueError('Операция требует проверки в 3x-ui! Возможен частичный успех. Заявку не повторять автоматически.') from exc
            self.store.operation_stage(request_id,'complete')
            self.store.transition(request_id,'processing','approved',result,admin)
            self.store.audit(admin,req['tg_id'],'approved',f'Заявка #{request_id} исполнена: {req["kind"]}')
            return req,result

    async def _execute(self,req,plan):
        kind=req['kind']; email=plan['email']; rid=req['id']
        if kind=='create':
            client={'email':email,'tgId':plan['tg_id'],'expiryTime':plan['expiry_at'] if plan.get('expiry_at') is not None else replace_expiry(plan['days'],plan['timestamp']),
                    'totalGB':gb_bytes(plan['gb']),'limitHwid':validate_hwid(plan['hwid']),
                    'enable':True, 'comment':('@'+(self.store.user(plan['tg_id']) or {}).get('username','')) if (self.store.user(plan['tg_id']) or {}).get('username') else ('Telegram ID '+str(plan['tg_id']))}
            self.store.operation_stage(rid,'create_started')
            await self.api.create(client,tuple(plan['inbounds']))
            self.store.operation_stage(rid,'create_sent')
            after=await self.api.get(email)
            if after.get('email')!=email or int(after.get('tgId') or 0)!=plan['tg_id']:
                raise XuiError('Новый клиент создан не в ожидаемом формате')
            for key in ('expiryTime','totalGB','limitHwid'):
                if int(after.get(key) or 0)!=int(client[key]):
                    raise XuiError(f'Не совпадает поле {key}')
            if not after.get('subId') or not set(plan['inbounds']).issubset(set(after.get('inboundIds') or [])):
                raise XuiError('Не удалось подтвердить подписку и inbound-привязки')
            self.store.bind(plan['tg_id'],email,plan.get('display_name') or '')
            return 'Создана подписка '+email
        if kind in ('renew','hwid_slots','adjust','block','unblock','delete'):
            before=await self.api.get(email)
            if before.get('subId')!=plan['pre_sub']:
                raise XuiError('Изменился subId; требуется ручная проверка')
            # Recheck client fields to detect concurrent edits in the web panel.
            for field,original in plan.get('pre_public',{}).items():
                if before.get(field)!=original:
                    raise XuiError(f'Параметр {field} изменился после рассмотрения заявки; операция отменена')
            if kind=='delete':
                self.store.operation_stage(rid,'delete_started')
                await self.api.delete_client(email)
                self.store.operation_stage(rid,'delete_returned')
                # Check all clients, as GET of missing client can itself report success:false.
                if any(c.get('email')==email for c in await self.api.clients()):
                    raise XuiError('Клиент остался в списке 3x-ui после удаления; нужна ручная сверка Inbound')
                self.store.unbind(req['tg_id'])
                return 'Клиент '+email+' удалён из 3x-ui по решению администратора'
            if kind=='renew':
                changes={'expiryTime':plan['expiry'],'totalGB':plan['gb']}
                if plan['hwid_explicit']:
                    changes['limitHwid']=validate_hwid(plan['hwid'])
                # Panel-level disable reason is not knowable via API; require explicit reactivation approval.
                binding=self.store.binding(req['tg_id'])
                if before.get('enable') is False and not binding['manual_block']:
                    if not plan.get('reactivate'):
                        raise XuiError('Клиент отключён; нужно отдельное решение администратора о восстановлении')
                    changes['enable']=True
            elif kind=='hwid_slots':
                changes={'limitHwid':plan['hwid']}
            else:
                changes=plan['changes']
            # Mark intent *before* the external call. A failed response can
            # still mean some inbounds were updated; never retry blindly.
            self.store.operation_stage(rid,'update_started')
            await self.api.update(email,before,changes)
            self.store.operation_stage(rid,'update_returned')
            after=await self.api.get(email)
            for field,value in changes.items():
                if after.get(field)!=value: raise XuiError(f'Не совпадает поле {field}')
            for field in ('subId','uuid','id','password','auth'):
                if before.get(field)!=after.get(field):
                    raise XuiError(f'Секретное поле {field} неожиданно изменилось')
            if before.get('inboundIds') != after.get('inboundIds'):
                raise XuiError('Список inbound изменился')
            for field in ('limitIp','reset','resetDay','resetWeekday','resetMax','trafficReset',
                          'trafficResetDay','group','comment','tgId','flow','security','reverse',
                          'allowedIPs','forwardedPorts','keepAlive','preSharedKey','privateKey','publicKey'):
                if field in before and field not in changes and before.get(field)!=after.get(field):
                    raise XuiError(f'Неожиданно изменилось поле {field}; нужна проверка в панели')
            if kind=='renew':
                # Journal stage written BEFORE external call; never automatically repeat ambiguous reset.
                self.store.operation_stage(rid,'reset_started')
                await self.api.reset_traffic(email)
                self.store.operation_stage(rid,'reset_returned')
                # Counters can increment immediately; do not insist on exact zero under traffic.
                await self.api.get(email)
            if kind in ('block','unblock'):
                self.store.set_block(req['tg_id'], kind=='block')
            return 'Клиент '+email+' обновлён ('+kind+')' 
        if kind=='hwid_delete':
            devices=await self.api.hwids(email)
            if not any(int(d.get('id',0))==plan['device_id'] for d in devices):
                raise XuiError('Запись HWID изменилась после рассмотрения заявки')
            self.store.operation_stage(rid,'hwid_delete_started')
            await self.api.delete_hwid(email,plan['device_id'])
            devices=await self.api.hwids(email)
            if any(int(d.get('id',0))==plan['device_id'] for d in devices):
                raise XuiError('HWID остался в списке')
            return 'Удалён HWID #'+str(plan['device_id'])
        raise ValueError('Неизвестная операция')

    def admin_request(self,tg_id:int,kind:str,settings:dict|None=None)->int:
        if kind not in ('adjust','block','unblock','delete'):
            raise ValueError('Неизвестное административное действие')
        if not self.store.binding(tg_id):
            raise ValueError('У пользователя нет привязки')
        rid=self.store.create_request(tg_id,kind)
        if settings:
            self.store.set_settings(rid,**settings)
        return rid
