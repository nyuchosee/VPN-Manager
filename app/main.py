"""Telegram VPN Manager, aiogram v3. Run: python -m app.main"""
from __future__ import annotations
import asyncio
import logging
from collections import Counter

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import Config
from app.domain import USER_DAYS, date_str, remaining, status, exact_expiry
from app.service import Service
from app.storage import Store
from app.ui import SingleScreen
from app.xui import XuiAPI
from app.tickets import TicketRelay
from app.security import validate_alias, safe_label
from app.limiting import ThrottleMiddleware

LOG=logging.getLogger('vpn-manager')
router=Router()
# Sensitive links and administrative controls must never appear in group chats.
router.message.filter(F.chat.type == 'private')
router.callback_query.filter(F.message.chat.type == 'private')
CFG:Config
STORE:Store
SERVICE:Service
BOT:Bot
UI:SingleScreen
TICKETS:TicketRelay


async def view(message:Message, text:str, reply_markup=None, *, force_new:bool=False):
    return await UI.show(message,text,reply_markup,force_new=force_new)


def kb(rows:list[list[tuple[str,str]]]):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label,callback_data=data) for label,data in row] for row in rows
    ])


def is_admin(tg_id:int)->bool:
    return tg_id in CFG.admin_ids


def require_admin(tg_id:int):
    if not is_admin(tg_id):
        raise ValueError('Недостаточно полномочий')


async def refresh_user(from_user):
    STORE.upsert_user(from_user.id,from_user.username or '',from_user.first_name or '')


async def tell_admins(text:str,rid:int|None=None):
    markup=kb([[('Открыть заявку',f'a:open:{rid}')]]) if rid else None
    for uid in CFG.admin_ids:
        try:
            await BOT.send_message(uid,text,reply_markup=markup)
        except Exception:
            LOG.warning('Не удалось доставить сообщение администратору %s',uid)


async def tell_user(tg_id:int,text:str):
    try: await BOT.send_message(tg_id,text,reply_markup=kb([[('Открыть меню','u:home')]]))
    except Exception: LOG.warning('Не удалось уведомить пользователя %s',tg_id)


async def home(message:Message, user_id:int|None=None, *, fresh:bool=False):
    uid=user_id if user_id is not None else message.from_user.id
    bound=STORE.binding(uid)
    if not bound:
        rows=[[('🆕 Получить доступ','u:new')],[('📝 Мои заявки','u:history')]]
    else:
        rows=[[('📋 Моя подписка','u:sub')],[('📱 Мои устройства','u:devices')],
              [('📅 Продление','u:renew')],[('📝 Мои заявки','u:history')]]
    rows.append([('💬 Поддержка','u:support')])
    await view(message,'Главное меню VPN Manager',reply_markup=kb(rows),force_new=fresh)


@router.message(CommandStart())
async def cmd_start(message:Message):
    await refresh_user(message.from_user)
    # /start is the explicit recovery point after Telegram chat history is
    # cleared. Never reuse a possibly invisible cached menu message.
    await home(message,fresh=True)


@router.message(Command('id'))
async def cmd_id(message:Message):
    await view(message,f'Ваш Telegram ID: {message.from_user.id}')


@router.message(Command('help'))
async def cmd_help(message:Message):
    await refresh_user(message.from_user)
    await home(message,fresh=True)


@router.callback_query(F.data == 'u:home')
async def back(callback:CallbackQuery):
    await callback.answer()
    await refresh_user(callback.from_user)
    await home(callback.message,callback.from_user.id)


@router.callback_query(F.data=='u:new')
async def first_access(callback:CallbackQuery):
    await callback.answer()
    await refresh_user(callback.from_user)
    if STORE.binding(callback.from_user.id):
        await view(callback.message,'Аккаунт уже создан. Используйте продление.');return
    await view(callback.message,'Первоначальная подписка. Выберите желаемый срок:',
         reply_markup=kb([[ (f'{d} дней',f'u:send:create:{d}') for d in USER_DAYS ]]))


@router.callback_query(F.data=='u:renew')
async def renew_menu(callback:CallbackQuery):
    await callback.answer()
    await refresh_user(callback.from_user)
    if not STORE.binding(callback.from_user.id):
        await view(callback.message,'Сначала необходимо получить доступ.');return
    await view(callback.message,'Запрос продления. Выберите желаемый срок:',
        reply_markup=kb([[(f'{d} дней',f'u:send:renew:{d}') for d in USER_DAYS]]))


@router.callback_query(F.data.startswith('u:send:'))
async def submit_days(callback:CallbackQuery):
    await callback.answer()
    await refresh_user(callback.from_user)
    try:
        _,_,kind,days=callback.data.split(':')
        days=int(days)
        if kind=='create': rid=SERVICE.create_user_request(callback.from_user.id,days)
        elif kind=='renew': rid=SERVICE.renew_request(callback.from_user.id,days)
        else: raise ValueError('Неизвестное действие')
        await view(callback.message,f'Заявка №{rid} на {days} дней отправлена администратору.')
        await tell_admins(f'Новая заявка №{rid}: {kind}, пользователь {callback.from_user.id}, {days} дней',rid)
    except (ValueError,TypeError) as exc:
        await view(callback.message,str(exc))


@router.callback_query(F.data=='u:sub')
async def my_subscription(callback:CallbackQuery):
    await callback.answer()
    await refresh_user(callback.from_user)
    bound=STORE.binding(callback.from_user.id)
    if not bound:
        await view(callback.message,'Подписка ещё не создана.');return
    try:
        c=await SERVICE.client_for(callback.from_user.id)
        hwids=await SERVICE.api.hwids(bound['email'])
        lim=int(c.get('limitHwid') or 0)
        device_text=f'{len(hwids)} из {lim}' if lim else 'Без ограничений'
        link=await SERVICE.link_for(callback.from_user.id,CFG.subscription_template)
        stat='Заблокирована администратором' if bound['manual_block'] else status(c)
        text=(f'📋 Моя подписка\nСтатус: {stat}\nДата окончания: {date_str(int(c.get("expiryTime") or 0))}'
              f'\nОсталось: {remaining(int(c.get("expiryTime") or 0))}'
              f'\nЗарегистрированные устройства: {device_text}\n')
        for d in hwids[:15]:
            name=d.get('deviceModel') or d.get('deviceOs') or 'Неизвестное устройство'
            text+=f'• {name} · {d.get("userAgent") or "Клиент не указан"}\n'
        if link:
            text+='\n🔗 Ссылка подписки:\n'+link
        else:
            text+='\nСсылка пока недоступна. Обратитесь к администратору.'
        await view(callback.message,text,
              reply_markup=kb([[('📱 Устройства','u:devices'),('📅 Продлить','u:renew')], [('🔄 Обновить','u:sub')]]))
    except Exception:
        LOG.exception('Не удалось получить сведения об аккаунте %s',callback.from_user.id)
        await view(callback.message,'Не удалось получить актуальные данные 3x-ui. Повторите позднее.')


@router.callback_query(F.data=='u:devices')
async def user_devices(callback:CallbackQuery):
    await callback.answer()
    await refresh_user(callback.from_user)
    bound=STORE.binding(callback.from_user.id)
    if not bound:
        await view(callback.message,'Подписка не найдена.');return
    try:
        c=await SERVICE.client_for(callback.from_user.id)
        devices=await SERVICE.api.hwids(bound['email'])
    except Exception:
        await view(callback.message,'Не удалось загрузить устройства из 3x-ui.');return
    limit=int(c.get('limitHwid') or 0)
    msg=f'📱 Зарегистрированные устройства: {len(devices)} / {limit if limit else "без лимита"}\n'
    rows=[]
    for d in devices[:20]:
        name=d.get('deviceModel') or d.get('deviceOs') or 'Устройство'
        msg+=f'• №{d["id"]}: {name}\n'
        rows.append([(f'🗑 Запросить удаление №{d["id"]}',f'u:del:{d["id"]}')])
    rows.extend([[('➕ +1 устройство','u:slots:1'),('➕ +2 устройства','u:slots:2')],
                [('➕ +3 устройства','u:slots:3')]])
    await view(callback.message,msg,reply_markup=kb(rows))


@router.callback_query(F.data.startswith('u:del:'))
async def request_device_delete(callback:CallbackQuery):
    await callback.answer()
    await refresh_user(callback.from_user)
    try:
        device_id=int(callback.data.split(':')[-1])
        bound=STORE.binding(callback.from_user.id)
        if not bound: raise ValueError('Нет подписки')
        current=await SERVICE.api.hwids(bound['email'])
        if not any(int(d.get('id',0))==device_id for d in current):
            raise ValueError('Устройство не найдено; обновите список')
        rid=SERVICE.device_request(callback.from_user.id,device_id)
        await view(callback.message,f'Заявка №{rid} на удаление HWID #{device_id} отправлена.')
        await tell_admins(f'Заявка №{rid}: удаление HWID #{device_id} для {callback.from_user.id}',rid)
    except (ValueError,TypeError) as exc:
        await view(callback.message,str(exc))


@router.callback_query(F.data.startswith('u:slots:'))
async def request_slots(callback:CallbackQuery):
    await callback.answer()
    await refresh_user(callback.from_user)
    try:
        amount=int(callback.data.split(':')[-1])
        rid=SERVICE.slots_request(callback.from_user.id,amount)
        await view(callback.message,f'Заявка №{rid} на дополнительные устройства отправлена.')
        await tell_admins(f'Заявка №{rid}: дополнительные слоты +{amount}, пользователь {callback.from_user.id}',rid)
    except (ValueError,TypeError) as exc:
        await view(callback.message,str(exc))


@router.callback_query(F.data=='u:history')
async def history(callback:CallbackQuery):
    await callback.answer()
    rows=STORE.requests_for(callback.from_user.id)
    text='Ваши заявки:\n'+('\n'.join(f'№{r["id"]} · {r["kind"]} · {r["status"]}' for r in rows) or 'Пока нет заявок')
    await view(callback.message,text)


class SupportInput(StatesGroup):
    message=State()


class TicketAdminInput(StatesGroup):
    message=State()


async def ticket_screen(message:Message,ticket_id:int,viewer_id:int):
    ticket=STORE.ticket(ticket_id)
    if not ticket or (viewer_id!=ticket['tg_id'] and not is_admin(viewer_id)):
        await view(message,'Тикет не найден');return
    title=f'💬 Тикет №{ticket_id} · {"Открыт" if ticket["status"]=="open" else "Закрыт"}\nПользователь: {ticket["tg_id"]}'
    if ticket['status']=='open':
        rows=[[('✉️ Ответить',f't:reply:{ticket_id}')], [('✅ Закрыть и очистить переписку',f't:close:{ticket_id}')]]
    else:
        rows=[]
    await view(message,title,reply_markup=kb(rows) if rows else None)
    if ticket['status']=='open':
        screen_id=STORE.ui_screen(message.chat.id)
        if screen_id:
            TICKETS.track(ticket_id,message.chat.id,screen_id)


@router.callback_query(F.data=='u:support')
async def support(callback:CallbackQuery,state:FSMContext):
    await callback.answer()
    await refresh_user(callback.from_user)
    previously_open=STORE.open_ticket(callback.from_user.id)
    ticket_id=STORE.create_ticket(callback.from_user.id)
    await state.set_state(SupportInput.message)
    await state.update_data(ticket_id=ticket_id)
    await ticket_screen(callback.message,ticket_id,callback.from_user.id)
    # Multiple support clicks shouldn't broadcast duplicate notices: a repeated
    # notification is harmless, but never create a second open ticket.
    if not previously_open:
        await TICKETS.notify_admins(ticket_id,
        f'💬 Тикет №{ticket_id} от Telegram {callback.from_user.id} (@{callback.from_user.username or "нет"})',
        kb([[('Открыть тикет',f't:open:{ticket_id}')]]))


@router.message(Command('cancel'), SupportInput.message)
async def cancel_support(message:Message,state:FSMContext):
    await state.clear()
    await view(message,'Режим переписки приостановлен. Тикет остаётся открытым; продолжить можно через «Поддержка».')


@router.message(Command('cancel'), TicketAdminInput.message)
async def cancel_admin_ticket(message:Message,state:FSMContext):
    await state.clear()
    await view(message,'Режим ответа приостановлен. Тикет остаётся открытым.')


@router.message(SupportInput.message)
async def support_message(message:Message,state:FSMContext):
    data=await state.get_data()
    ticket_id=data.get('ticket_id')
    ticket=STORE.ticket(ticket_id) if ticket_id is not None else None
    if message.text and message.text.split()[0].lower()=='/close_ticket':
        await close_ticket_command(message,state)
        return
    if message.text and message.text.startswith('/'):
        await state.clear()
        await view(message,'Режим ответа приостановлен. Тикет остаётся открытым.')
        return
    if not ticket or ticket['status']!='open' or ticket['tg_id']!=message.from_user.id:
        await state.clear()
        await view(message,'Тикет закрыт. Создайте новый через «Поддержка».')
        return
    if message.text and len(message.text)>3000:
        await view(message,'Сообщение слишком длинное (не более 3000 символов).')
        return
    # Copy text/media using Telegram; do not retain message contents locally.
    try:
        count=await TICKETS.relay(ticket_id,message,CFG.admin_ids)
    except ValueError as exc:
        await view(message,str(exc)); return
    if not count:
        await view(message,'Не удалось доставить сообщение администратору; проверьте доступность бота.')


@router.message(TicketAdminInput.message)
async def ticket_admin_message(message:Message,state:FSMContext):
    if not is_admin(message.from_user.id):return
    data=await state.get_data()
    ticket_id=data.get('ticket_id')
    ticket=STORE.ticket(ticket_id) if ticket_id is not None else None
    if message.text and message.text.startswith('/'):
        command=message.text.split()[0].lower()
        await state.clear()
        if command=='/ticket':await admin_ticket(message)
        elif command=='/tickets':await admin_tickets(message)
        elif command=='/close_ticket':await close_ticket_command(message,state)
        else:await view(message,'Режим ответа приостановлен. Тикет остаётся открытым.')
        return
    if not ticket or ticket['status']!='open':
        await state.clear()
        await view(message,'Тикет закрыт. Перейдите в /tickets.')
        return
    if message.text and len(message.text)>3000:
        await view(message,'Сообщение слишком длинное (не более 3000 символов).')
        return
    try:
        count=await TICKETS.relay(ticket_id,message,[ticket['tg_id']])
    except ValueError as exc:
        await view(message,str(exc)); return
    if not count:
        await view(message,'Сообщение не доставлено пользователю; попробуйте ещё раз.')


@router.callback_query(F.data.startswith('t:open:'))
async def open_ticket(callback:CallbackQuery):
    await callback.answer()
    tid=int(callback.data.split(':')[-1]);ticket=STORE.ticket(tid)
    if not ticket or (not is_admin(callback.from_user.id) and ticket['tg_id']!=callback.from_user.id):
        await callback.answer('Нет доступа',show_alert=True);return
    await ticket_screen(callback.message,tid,callback.from_user.id)


@router.callback_query(F.data.startswith('t:reply:'))
async def reply_in_ticket(callback:CallbackQuery,state:FSMContext):
    await callback.answer()
    tid=int(callback.data.split(':')[-1]);ticket=STORE.ticket(tid)
    if not ticket or ticket['status']!='open' or (not is_admin(callback.from_user.id) and callback.from_user.id!=ticket['tg_id']):
        await callback.answer('Тикет закрыт или недоступен',show_alert=True);return
    await state.set_state(TicketAdminInput.message if is_admin(callback.from_user.id) else SupportInput.message)
    await state.update_data(ticket_id=tid)
    await ticket_screen(callback.message,tid,callback.from_user.id)


async def close_ticket_action(ticket_id:int,actor:int):
    ticket=STORE.ticket(ticket_id)
    if not ticket or (actor!=ticket['tg_id'] and not is_admin(actor)):
        raise ValueError('Тикет недоступен')
    if ticket['status']=='open':
        if not STORE.close_ticket(ticket_id,actor):
            raise ValueError('Тикет уже закрыт')
        STORE.audit(actor,ticket['tg_id'],'ticket_closed',f'Тикет #{ticket_id} закрыт')
    return await TICKETS.cleanup(ticket_id)


@router.callback_query(F.data.startswith('t:close:'))
async def close_ticket_callback(callback:CallbackQuery,state:FSMContext):
    tid=int(callback.data.split(':')[-1])
    try:
        removed,failed=await close_ticket_action(tid,callback.from_user.id)
    except ValueError as exc:
        await callback.answer(str(exc),show_alert=True);return
    await callback.answer('Тикет закрыт')
    await state.clear()
    msg=f'Тикет №{tid} закрыт. Удалено сообщений: {removed}.'
    if failed:msg+=f' Не удалось удалить: {failed} (ограничения Telegram или ошибки API).'
    await view(callback.message,msg)


@router.message(Command('tickets'))
async def admin_tickets(message:Message):
    if not is_admin(message.from_user.id):return
    tickets=STORE.tickets_open()
    rows=[[(f'#{t["id"]} · Telegram {t["tg_id"]}',f't:open:{t["id"]}')] for t in tickets]
    await view(message,'Открытые тикеты:' if rows else 'Открытых тикетов нет.',reply_markup=kb(rows) if rows else None)


@router.message(Command('ticket'))
async def admin_ticket(message:Message):
    if not is_admin(message.from_user.id):return
    try:
        tid=int((message.text or '').split()[1]);await ticket_screen(message,tid,message.from_user.id)
    except (ValueError,IndexError):await view(message,'Использование: /ticket ID')


@router.message(Command('ticket_cleanup'))
async def admin_cleanup_ticket(message:Message):
    if not is_admin(message.from_user.id):return
    try:
        tid=int((message.text or '').split()[1]);ticket=STORE.ticket(tid)
        if not ticket or ticket['status']!='closed':raise ValueError('Тикет должен быть закрыт')
        removed,failed=await TICKETS.cleanup(tid)
        await view(message,f'Тикет #{tid}: удалено {removed}, недоступно удаление {failed}.')
    except (ValueError,IndexError) as exc:await view(message,f'/ticket_cleanup ID\n{exc}')


@router.message(Command('close_ticket'))
async def close_ticket_command(message:Message,state:FSMContext):
    if is_admin(message.from_user.id):
        try: tid=int((message.text or '').split()[1])
        except (ValueError,IndexError):await view(message,'Администратор: /close_ticket ID');return
    else:
        ticket=STORE.open_ticket(message.from_user.id)
        if not ticket:await view(message,'Нет открытого тикета');return
        tid=ticket['id']
    try:
        removed,failed=await close_ticket_action(tid,message.from_user.id)
        await state.clear()
        await view(message,f'Тикет #{tid} закрыт. Удалено: {removed}. Недоступно удаление: {failed}.')
    except ValueError as exc:await view(message,str(exc))


@router.message(Command('lookup'))
async def admin_lookup(message:Message):
    if not is_admin(message.from_user.id):return
    parts=(message.text or '').split(maxsplit=1)
    if len(parts)<2 or not 2<=len(parts[1])<=60:
        await view(message,'Использование: /lookup часть_имени');return
    try:
        clients=await SERVICE.api.clients()
        matches=[c.get('email','') for c in clients if parts[1].lower() in c.get('email','').lower()][:30]
        await view(message,'Найдено:\n'+ ('\n'.join(matches) or 'Совпадений нет'))
    except Exception:
        await view(message,'API поиска временно недоступно')


@router.message(Command('admin'))
async def admin_home(message:Message):
    if not is_admin(message.from_user.id): return
    rows=[[('📥 Заявки','a:list')],[('👥 Клиенты','a:clients')],[('💬 Тикеты','a:tickets')],[('📋 Журнал','a:logs')]]
    await view(message,'Панель администратора.\nКоманды: /bind, /grant, /client, /ops, /diag, /resolve, /helpadmin',reply_markup=kb(rows),force_new=True)


@router.message(Command('unlink_missing'))
async def admin_unlink_missing(message:Message):
    """Unlink only when a *live* panel list proves client is absent.

    Deleting from 3x-ui UI doesn't remove the bot's SQLite binding. Keep
    Telegram-user identity and history; never touch panel accounts here.
    """
    if not is_admin(message.from_user.id):
        return
    try:
        parts=(message.text or '').split()
        if len(parts)!=2 or not parts[1].isdigit() or int(parts[1])<=0:
            raise ValueError('Использование: /unlink_missing TG_ID')
        tg_id=int(parts[1])
        bound=STORE.binding(tg_id)
        if not bound:
            await view(message,'Привязка этого Telegram ID отсутствует.')
            return
        if STORE.pending_others(tg_id,-1):
            raise ValueError('Есть незавершённые заявки. Сначала разрешите их вручную.')
        clients=await SERVICE.api.clients()
        if any(not isinstance(c,dict) or not isinstance(c.get('email'),str) for c in clients):
            raise ValueError('Неожиданный формат списка 3x-ui; привязка сохранена')
        email=bound['email']
        if any(c['email']==email or str(c.get('tgId') or '')==str(tg_id) for c in clients):
            raise ValueError('В 3x-ui ещё существует клиент с этим email или Telegram ID. Привязка сохранена')
        STORE.unbind(tg_id)
        STORE.audit(message.from_user.id,tg_id,'unlink_missing',
                    'Снята устаревшая Telegram-привязка после проверки отсутствия клиента в 3x-ui')
        await view(message,f'Привязка TG ID {tg_id} снята. Клиенты 3x-ui не изменены. Пользователь может отправить /start.')
    except Exception as exc:
        await view(message,f'Привязка не изменена: {exc}')


@router.message(Command('helpadmin'))
async def admin_help(message:Message):
    if not is_admin(message.from_user.id):return
    await view(message,'''Команды администратора:
/admin — панель
/client TG_ID — карточка клиента
/alias TG_ID ИМЯ — изменить отображаемый псевдоним (не меняет 3x-ui)
/lookup ИМЯ — поиск в панели
/bind TG_ID CLIENT_EMAIL — привязать существующую запись без изменения 3x-ui\n/unlink_missing TG_ID — снять привязку после проверки удаления клиента из 3x-ui
/grant TG_ID 30 — создать заявку первичной выдачи и настроить её
/ops — операции, требующие ручной проверки
/diag — безопасная диагностика
/tickets — открытые тикеты; /ticket ID — открыть тикет\n/close_ticket ID — закрыть и удалить переписку; /ticket_cleanup ID — повторить очистку
/resolve REQUEST_ID approved|rejected — после сверки состояния в панели
/writecheck — показать режим операций

Для изменения параметров действующего клиента откройте /client TG_ID → «Изменить параметры».
Числа и специальные значения вводятся через кнопки в карточке заявки.''')


@router.message(Command('diag'))
async def admin_diag(message:Message):
    if not is_admin(message.from_user.id): return
    pending=STORE.unfinished()
    lines=['🩺 VPN Manager — диагностика (без изменения 3x-ui)',
           'Запись: '+('ВКЛЮЧЕНА' if CFG.write_enabled else 'ОТКЛЮЧЕНА'),
           'Выбранные Inbound: '+','.join(map(str,CFG.inbound_ids)),
           'Незавершённых операций: '+str(len(pending))]
    for r in pending[:12]:
        lines.append(f'№{r["request_id"]} · {r["kind"]} · {r["stage"]}'
                     f' (последний этап: {r.get("last_progress_stage") or "неизвестен"})')
    lines.append('Полный аудит запуска: sudo vpn-manager-doctor --api')
    await view(message,'\n'.join(lines))


@router.message(Command('writecheck'))
async def write_check(message:Message):
    if is_admin(message.from_user.id):
        await view(message,'Изменения 3x-ui: '+('РАЗРЕШЕНЫ (WRITE_ENABLED=true)' if CFG.write_enabled else 'ОТКЛЮЧЕНЫ (безопасный режим чтения)'))


@router.callback_query(F.data == 'a:home')
async def admin_back(callback:CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer('Нет доступа',show_alert=True)
        return
    await callback.answer()
    await view(callback.message,'Панель администратора.\nКоманды: /bind, /grant, /client, /ops, /diag, /resolve, /helpadmin',
         reply_markup=kb([[('📥 Заявки','a:list')],[('👥 Клиенты','a:clients')],[('💬 Тикеты','a:tickets')],[('📋 Журнал','a:logs')]]))


@router.callback_query(F.data=='a:list')
async def pending(callback:CallbackQuery):
    if not is_admin(callback.from_user.id):await callback.answer('Нет доступа',show_alert=True);return
    await callback.answer()
    requests=STORE.pending()
    rows=[[(f'#{r["id"]} · {r["kind"]} · {r["status"]}', f'a:open:{r["id"]}')] for r in requests[:25]]
    await view(callback.message,'Заявки:' if rows else 'Нет открытых заявок.',reply_markup=kb(rows) if rows else None)


@router.callback_query(F.data=='a:tickets')
async def admin_tickets_button(callback:CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer('Нет доступа',show_alert=True);return
    await callback.answer()
    tickets=STORE.tickets_open()
    rows=[[(f'#{t["id"]} · Telegram {t["tg_id"]}',f't:open:{t["id"]}')] for t in tickets]
    await view(callback.message,'Открытые тикеты:' if rows else 'Открытых тикетов нет.',reply_markup=kb(rows) if rows else None)


@router.callback_query(F.data=='a:clients')
async def client_list(callback:CallbackQuery):
    if not is_admin(callback.from_user.id):await callback.answer('Нет доступа',show_alert=True);return
    await callback.answer()
    rows=[]
    for b in STORE.bindings()[:30]:
        label=safe_label(b.get('display_name') or b['email'],30)
        email=safe_label(b['email'],35)
        desc=f'{label} · {email} · ID {b["tg_id"]}' if label!=email else f'{email} · {b["tg_id"]}'
        rows.append([(desc[:64],f'a:client:{b["tg_id"]}')])
    await view(callback.message,'Привязанные пользователи:' if rows else 'Пока нет привязок.',reply_markup=kb(rows) if rows else None)


@router.message(Command('client'))
async def command_client(message:Message):
    if not is_admin(message.from_user.id):return
    try:
        tg_id=int((message.text or '').split()[1])
        await show_client(message,tg_id)
    except (IndexError,ValueError):
        await view(message,'Формат: /client 123456789')


@router.callback_query(F.data.startswith('a:client:'))
async def callback_client(callback:CallbackQuery):
    if not is_admin(callback.from_user.id):await callback.answer('Нет доступа',show_alert=True);return
    await callback.answer()
    await show_client(callback.message,int(callback.data.split(':')[-1]))


async def show_client(message:Message,tg_id:int):
    b=STORE.binding(tg_id)
    if not b:
        await view(message,'Клиент не привязан к Telegram.');return
    try:
        c=await SERVICE.client_for(tg_id)
        devices=await SERVICE.api.hwids(b['email'])
    except Exception:
        await view(message,'Не удалось загрузить клиента через API.');return
    gb=int(c.get('totalGB') or 0)
    lim=int(c.get('limitHwid') or 0)
    display=safe_label(b.get('display_name'),48)
    text=(f'👤 {display + " · " if display else ""}{safe_label(b["email"],65)} · Telegram {tg_id}\nСтатус: {status(c)}' 
          f'\nСрок: {date_str(int(c.get("expiryTime") or 0))}'
          f'\nТрафик: {"безлимит" if gb==0 else f"{gb/1024**3:.1f} ГБ"}'
          f'\nHWID: {len(devices)}/{lim if lim else "без лимита"}'
          f'\nАдмин-блокировка: {"да" if b["manual_block"] else "нет"}')
    rows=[[('🏷 Изменить псевдоним',f'a:alias:{tg_id}')],
          [('✏️ Изменить параметры',f'a:make:adjust:{tg_id}')],
          [('🚫 Заблокировать',f'a:make:block:{tg_id}'),('✅ Разблокировать',f'a:make:unblock:{tg_id}')],
          [('🗑 Удалить клиента из 3x-ui',f'a:make:delete:{tg_id}')]]
    for dev in devices[:15]:
        rows.append([(f'🗑 HWID #{dev["id"]} · {(dev.get("deviceModel") or "устройство")[:22]}',f'a:directhwid:{tg_id}:{dev["id"]}')])
    await view(message,text,reply_markup=kb(rows))


class AliasInput(StatesGroup):
    value=State()


@router.callback_query(F.data.startswith('a:alias:'))
async def begin_alias_edit(callback:CallbackQuery,state:FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer('Нет доступа',show_alert=True);return
    await callback.answer()
    try:
        uid=int(callback.data.split(':')[-1])
        if uid<=0 or not STORE.binding(uid):raise ValueError('Привязка не найдена')
        await state.set_state(AliasInput.value)
        await state.update_data(tg_id=uid)
        await view(callback.message,'Введите отображаемый псевдоним (1–48 символов, допускается кириллица). /cancel — отмена.')
    except (ValueError,TypeError) as exc:
        await view(callback.message,str(exc))


@router.message(Command('alias'))
async def command_alias(message:Message):
    if not is_admin(message.from_user.id):return
    try:
        fields=(message.text or '').split(maxsplit=2)
        if len(fields)!=3 or not fields[1].isdigit() or int(fields[1])<=0:
            raise ValueError('Формат: /alias TG_ID Псевдоним')
        tg_id=int(fields[1]);label=STORE.set_display_name(tg_id,fields[2])
        STORE.audit(message.from_user.id,tg_id,'alias_changed','Обновлён отображаемый псевдоним')
        await view(message,f'Псевдоним для Telegram {tg_id}: {label}. Имя в 3x-ui не изменено.')
    except ValueError as exc:
        await view(message,f'Ошибка: {exc}')


@router.message(AliasInput.value)
async def receive_alias_edit(message:Message,state:FSMContext):
    if not is_admin(message.from_user.id):return
    if (message.text or '').strip().lower()=='/cancel':
        await state.clear()
        await view(message,'Изменение псевдонима отменено.')
        return
    data=await state.get_data()
    try:
        uid=int(data['tg_id']);label=STORE.set_display_name(uid,message.text or '')
        STORE.audit(message.from_user.id,uid,'alias_changed','Обновлён отображаемый псевдоним')
        await state.clear()
        await show_client(message,uid)
    except (ValueError,KeyError) as exc:
        await view(message,f'Некорректный псевдоним: {exc}. Повторите ввод или /cancel.')


@router.message(Command('bind'))
async def admin_bind(message:Message):
    if not is_admin(message.from_user.id):return
    try:
        _,uid,email=(message.text or '').split(maxsplit=2)
        uid=int(uid)
        await SERVICE.register_existing(uid,email,message.from_user.id)
        await view(message,f'Привязано: {uid} → {email}. Панель 3x-ui не изменена.')
    except Exception as exc:
        await view(message,f'Формат: /bind TG_ID CLIENT_EMAIL\nОшибка: {exc}')


@router.message(Command('grant'))
async def admin_grant(message:Message):
    if not is_admin(message.from_user.id):return
    try:
        _,uid,days=(message.text or '').split()
        uid=int(uid);days=int(days)
        STORE.upsert_user(uid)
        rid=SERVICE.create_user_request(uid,days)
        await show_request(message,rid)
    except Exception as exc:
        await view(message,f'Формат: /grant TG_ID 7|30|90\n{exc}')


@router.callback_query(F.data.startswith('a:make:'))
async def admin_make(callback:CallbackQuery):
    if not is_admin(callback.from_user.id):await callback.answer('Нет доступа',show_alert=True);return
    await callback.answer()
    try:
        _,_,kind,uid=callback.data.split(':')
        rid=SERVICE.admin_request(int(uid),kind)
        await show_request(callback.message,rid)
    except Exception as exc:
        await view(callback.message,str(exc))


@router.callback_query(F.data.startswith('a:directhwid:'))
async def admin_direct_delete(callback:CallbackQuery):
    if not is_admin(callback.from_user.id):await callback.answer('Нет доступа',show_alert=True);return
    await callback.answer()
    try:
        _,_,uid,hwid=callback.data.split(':')
        rid=SERVICE.device_request(int(uid),int(hwid))
        await show_request(callback.message,rid)
    except Exception as exc:
        await view(callback.message,str(exc))


@router.callback_query(F.data.startswith('a:open:'))
async def open_request(callback:CallbackQuery):
    if not is_admin(callback.from_user.id):await callback.answer('Нет доступа',show_alert=True);return
    await callback.answer()
    await show_request(callback.message,int(callback.data.split(':')[-1]))


def read_settings(r):
    cfg=STORE.settings(r)
    term=cfg.get('days',r['requested_days'])
    traffic=cfg.get('traffic_gb',500)
    hwid=cfg.get('hwid_limit',3 if r['kind']=='create' else None)
    return cfg,term,traffic,hwid


async def show_request(message:Message,rid:int):
    r=STORE.request(rid)
    if not r:
        await view(message,'Заявка не найдена.');return
    u=STORE.user(r['tg_id']) or {}
    cfg,term,traffic,hwid=read_settings(r)
    exact=cfg.get('expiry_at')
    title=f'📥 Заявка #{rid} · {r["kind"]}\nСтатус: {r["status"]}\nПользователь: @{u.get("username") or "без username"} · ID {r["tg_id"]}'
    if r['kind']=='create': title+=f'\nИмя: {cfg.get("alias") or u.get("username") or "tg_"+str(r["tg_id"])}'
    if r['kind'] in ('create','renew','adjust'):
        title+=f'\nСрок: {date_str(exact) if exact else "не менять" if r["kind"]=="adjust" and "days" not in cfg else "бессрочно" if term is None else str(term)+" дней от одобрения"}'
        title+=f'\nТрафик: {"не менять" if r["kind"]=="adjust" and "traffic_gb" not in cfg else "безлимит" if traffic is None else str(traffic)+" ГБ"}'
        title+=f'\nHWID: {"не менять" if hwid is None and r["kind"]!="create" and "hwid_limit" not in cfg else "безлимит" if hwid is None else str(hwid)}'
    elif r['kind']=='hwid_delete': title+=f'\nУдаление HWID #{r["device_id"]}'
    elif r['kind']=='delete':
        title+='\n⚠️ БЕЗВОЗВРАТНОЕ УДАЛЕНИЕ клиента из ВСЕХ Inbound.\nКлючи, subId, HWID и привязка будут утрачены.'
        title+='\nДополнительное подтверждение: '+('ПОЛУЧЕНО' if cfg.get('delete_confirm') else 'НЕ ПОЛУЧЕНО')
    elif r['kind']=='hwid_slots':
        title+=f'\nДополнительных устройств: +{r["requested_slots"]}'
        if 'hwid_limit' in cfg: title+=f'\nИтоговый HWID Limit: {"безлимит" if cfg["hwid_limit"] is None else cfg["hwid_limit"]}'
    if cfg.get('reactivate'):title+='\nРазрешено восстановление отключённого клиента'
    if cfg.get('explicit_from_unlimited'):title+='\nРазрешён перевод с бессрочного на срочный'
    if r['status']!='pending':
        title+='\nРезультат: '+(r['result'] or '—')
        await view(message,title);return
    rows=[]
    if r['kind'] in ('create','renew','adjust'):
        rows += [[('7 дн',f'a:set:{rid}:days:7'),('30 дн',f'a:set:{rid}:days:30'),('90 дн',f'a:set:{rid}:days:90')],
                 [('♾ Бессрочно',f'a:set:{rid}:days:forever'),('✏️ Свой срок',f'a:input:{rid}:days')],
                 [('500 ГБ',f'a:set:{rid}:traffic_gb:500'),('♾ Трафик',f'a:set:{rid}:traffic_gb:unlimited')],
                 [('✏️ Свои ГБ',f'a:input:{rid}:traffic_gb')],
                 [('HWID 3',f'a:set:{rid}:hwid_limit:3'),('♾ HWID',f'a:set:{rid}:hwid_limit:unlimited')],
                 [('✏️ Свой HWID',f'a:input:{rid}:hwid_limit')]]
        if r['kind']=='create': rows.append([('✏️ Псевдоним',f'a:input:{rid}:alias')])
        title+='\n📅 Выбранный срок ЗАМЕНЯЕТ текущий, не суммируется с оставшимися днями.'
        rows.append([('📆 Точная дата окончания UTC',f'a:input:{rid}:expiry_at')])
        if r['kind'] in ('adjust','renew'):
            rows.append([('♾ → 📅 Подтвердить перевод в срочную',f'a:set:{rid}:explicit_from_unlimited:true')])
        if r['kind']=='adjust':
            rows.append([('Сбросить выбранные поля',f'a:clear:{rid}')])
    elif r['kind']=='hwid_slots':
        rows += [[('♾ HWID',f'a:set:{rid}:hwid_limit:unlimited'),('✏️ Итоговый лимит',f'a:input:{rid}:hwid_limit')]]
    if r['kind']=='renew' and STORE.binding(r['tg_id']):
        rows.append([('🔓 Разрешить включение после истечения',f'a:set:{rid}:reactivate:true')])
    if r['kind']=='delete':
        rows.append([('⚠️ Я подтверждаю полное удаление',f'a:set:{rid}:delete_confirm:true')])
        if cfg.get('delete_confirm'):
            rows.append([('🗑 ПОДТВЕРДИТЬ УДАЛЕНИЕ',f'a:approve:{rid}')])
    else:
        rows.append([('✅ Подтвердить',f'a:approve:{rid}')])
    rows.append([('❌ Отклонить',f'a:reject:{rid}')])
    await view(message,title,reply_markup=kb(rows))


@router.callback_query(F.data.startswith('a:set:'))
async def set_request_value(callback:CallbackQuery):
    if not is_admin(callback.from_user.id):await callback.answer('Нет доступа',show_alert=True);return
    await callback.answer()
    try:
        _,_,rid,field,value=callback.data.split(':')
        rid=int(rid)
        if field in ('days','traffic_gb','hwid_limit'):
            new=None if value in ('forever','unlimited') else int(value)
            STORE.set_settings(rid,**{field:new})
            if field=='days':STORE.remove_settings(rid,'expiry_at')
        elif field in ('explicit_from_unlimited','reactivate','delete_confirm'):
            STORE.set_settings(rid,**{field:True})
        else: raise ValueError('Неизвестный параметр')
        await show_request(callback.message,rid)
    except (ValueError,TypeError) as exc:
        await view(callback.message,str(exc))


@router.callback_query(F.data.startswith('a:clear:'))
async def clear_settings(callback:CallbackQuery):
    if not is_admin(callback.from_user.id):await callback.answer('Нет доступа',show_alert=True);return
    await callback.answer()
    rid=int(callback.data.split(':')[-1])
    req=STORE.request(rid)
    if req and req['status']=='pending':
        STORE.db.execute("UPDATE requests SET settings='{}' WHERE id=?",(rid,))
    await show_request(callback.message,rid)


class AdminInput(StatesGroup):
    value=State()


@router.callback_query(F.data.startswith('a:input:'))
async def begin_input(callback:CallbackQuery,state:FSMContext):
    if not is_admin(callback.from_user.id):await callback.answer('Нет доступа',show_alert=True);return
    await callback.answer()
    try:
        _,_,rid,field=callback.data.split(':')
        if field not in ('days','traffic_gb','hwid_limit','alias','expiry_at'):raise ValueError('Неизвестное поле')
        req=STORE.request(int(rid))
        if not req or req['status']!='pending':raise ValueError('Заявка больше не редактируется')
        await state.set_state(AdminInput.value)
        await state.update_data(rid=int(rid),field=field)
        await view(callback.message,('Дата в UTC: YYYY-MM-DD или YYYY-MM-DD HH:MM. Дата без времени означает 23:59 UTC.' if field=='expiry_at' else f'Отправьте значение {field}: положительное число либо псевдоним. /cancel для отмены.'))
    except (ValueError,TypeError) as exc:
        await view(callback.message,str(exc))


@router.message(Command('cancel'))
async def cancel_input(message:Message,state:FSMContext):
    await state.clear()
    await view(message,'Ввод отменён.')


@router.message(AdminInput.value)
async def receive_input(message:Message,state:FSMContext):
    if not is_admin(message.from_user.id):return
    data=await state.get_data()
    field=data.get('field'); rid=data.get('rid')
    value=(message.text or '').strip()
    try:
        if field=='expiry_at':
            value=exact_expiry(value)
        elif field=='alias':
            value=validate_alias(value)
        else:
            value=int(value)
            limits={'days':36500,'traffic_gb':1000000,'hwid_limit':1000}
            if not 1<=value<=limits[field]: raise ValueError('Число вне допустимого диапазона')
        if field=='days':
            STORE.set_settings(rid,days=value)
            STORE.remove_settings(rid,'expiry_at')
        elif field=='expiry_at':
            STORE.set_settings(rid,expiry_at=value)
            STORE.remove_settings(rid,'days')
        else:
            STORE.set_settings(rid,**{field:value})
        await state.clear()
        await show_request(message,rid)
    except (ValueError,TypeError) as exc:
        await view(message,f'Ошибка: {exc}. Повторите или /cancel')


@router.callback_query(F.data.startswith('a:approve:'))
async def approve(callback:CallbackQuery):
    if not is_admin(callback.from_user.id):await callback.answer('Нет доступа',show_alert=True);return
    await callback.answer('Выполняю проверку и операцию…')
    rid=int(callback.data.split(':')[-1])
    try:
        req,result=await SERVICE.approve(rid,callback.from_user.id)
        await view(callback.message,f'✅ Заявка №{rid}: {result}')
        await tell_user(req['tg_id'],f'✅ Заявка №{rid} одобрена. Откройте «Моя подписка» через /start.')
    except Exception as exc:
        await view(callback.message,f'Не удалось подтвердить №{rid}: {exc}')


@router.callback_query(F.data.startswith('a:reject:'))
async def reject(callback:CallbackQuery):
    if not is_admin(callback.from_user.id):await callback.answer('Нет доступа',show_alert=True);return
    await callback.answer()
    rid=int(callback.data.split(':')[-1])
    try:
        req=await SERVICE.reject(rid,callback.from_user.id)
        await view(callback.message,f'Заявка №{rid} отклонена.')
        await tell_user(req['tg_id'],f'Заявка №{rid} отклонена администратором.')
    except Exception as exc:
        await view(callback.message,str(exc))


@router.callback_query(F.data=='a:logs')
async def admin_logs(callback:CallbackQuery):
    if not is_admin(callback.from_user.id):await callback.answer('Нет доступа',show_alert=True);return
    await callback.answer()
    rows=STORE.audit_list()
    text='Последние действия:\n'+('\n'.join(f'{r["id"]}: {r["event"]} · {r["details"]}' for r in rows) or 'Пока нет')
    await view(callback.message,text[:3900])


@router.message(Command('ops'))
async def admin_ops(message:Message):
    if not is_admin(message.from_user.id):return
    rows=STORE.unfinished()
    text='Операции требующие проверки:\n'+('\n'.join(f'#{r["request_id"]} · {r["kind"]} · {r["email"]} · {r["stage"]} / {r.get("last_progress_stage") or "неизвестно"}' for r in rows) or 'Нет')
    await view(message,text[:3900])


@router.message(Command('resolve'))
async def admin_resolve(message:Message):
    if not is_admin(message.from_user.id):return
    try:
        _,rid,outcome=(message.text or '').split()
        rid=int(rid)
        req=STORE.resolve_manual(rid,message.from_user.id,outcome)
        STORE.operation_stage(rid,'complete_manual')
        await view(message,f'Заявка #{rid}: вручную помечена как {outcome}.')
        await tell_user(req['tg_id'],f'Заявка #{rid}: результат уточнён администратором. Откройте /start.')
    except Exception as exc:
        await view(message,f'Формат /resolve REQUEST_ID approved|rejected.\nОшибка: {exc}\nТолько после самостоятельной проверки записи в панели.')


async def main():
    global CFG,STORE,SERVICE,BOT,UI,TICKETS
    CFG=Config.from_env()
    STORE=Store(CFG.database_path)
    STORE.reconcile_stuck()
    api=XuiAPI(CFG.xui_url,CFG.xui_token,CFG.verify_tls,CFG.timeout)
    SERVICE=Service(STORE,api,CFG.inbound_ids,CFG.write_enabled)
    BOT=Bot(token=CFG.telegram_token)
    UI=SingleScreen(BOT,STORE,CFG.admin_ids)
    TICKETS=TicketRelay(BOT,STORE,CFG.admin_ids)
    dp=Dispatcher()
    # Require independent authorization in every admin handler; flood control
    # additionally limits messages and callback presses from all actors.
    limiter=ThrottleMiddleware(CFG.admin_ids)
    router.message.middleware(limiter)
    router.callback_query.middleware(limiter)
    dp.include_router(router)
    LOG.info('Бот запускается; изменения 3x-ui: %s',CFG.write_enabled)
    if STORE.unfinished():
        LOG.warning('Есть незавершённые операции: %s. Проверьте /ops до изменения клиентов.',len(STORE.unfinished()))
    try:
        await dp.start_polling(BOT,allowed_updates=dp.resolve_used_update_types(),
                               tasks_concurrency_limit=32)
    finally:
        await api.close()
        await BOT.session.close()
        STORE.close()


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    asyncio.run(main())
