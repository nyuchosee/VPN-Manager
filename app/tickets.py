"""Ephemeral support tickets: no message contents are persisted in SQLite.

Telegram only allows message deletion within ~48 hours. We record failed deletes
for audit/retry, and never claim all messages vanished if some cannot be deleted.
"""
from __future__ import annotations
import logging
try:
    from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
except ImportError:  # Permit isolated business-logic unit tests without Telegram SDK
    class TelegramBadRequest(Exception):
        pass
    class TelegramForbiddenError(Exception):
        pass

LOG = logging.getLogger('vpn-manager.tickets')

class TicketRelay:
    def __init__(self, bot, store, admin_ids):
        self.bot = bot
        self.store = store
        self.admin_ids = frozenset(admin_ids)

    def track(self, ticket_id: int, chat_id: int, message_id: int):
        self.store.track_ticket(ticket_id, chat_id, message_id)

    async def notify_admins(self, ticket_id: int, text: str, markup=None):
        for admin in self.admin_ids:
            try:
                message = await self.bot.send_message(admin, text, reply_markup=markup)
                self.track(ticket_id, admin, message.message_id)
            except Exception as exc:
                LOG.warning('Ticket %s notification to admin %s failed: %s', ticket_id, admin, type(exc).__name__)

    async def relay(self, ticket_id: int, source, targets):
        ticket = self.store.ticket(ticket_id)
        if not ticket or ticket['status'] != 'open':
            raise ValueError('Тикет уже закрыт')
        if self.store.recent_ticket_messages(ticket_id,source.chat.id)>=15:
            raise ValueError('Лимит сообщений в тикете: 15 в минуту. Повторите позже.')
        self.track(ticket_id, source.chat.id, source.message_id)
        delivered = 0
        for target in targets:
            if target == source.chat.id:
                continue
            try:
                sender = 'администратора' if source.chat.id in self.admin_ids else f'пользователя {source.chat.id}'
                prefix = f'💬 Тикет #{ticket_id} · сообщение от {sender}:\n'
                if getattr(source, 'text', None) and len(prefix + source.text) <= 4000:
                    message = await self.bot.send_message(target, prefix + source.text)
                    self.track(ticket_id, target, message.message_id)
                else:
                    marker = await self.bot.send_message(target, f'💬 Тикет #{ticket_id} · вложение от {sender}')
                    self.track(ticket_id, target, marker.message_id)
                    message = await self.bot.copy_message(target, source.chat.id, source.message_id)
                    self.track(ticket_id, target, message.message_id)
                delivered += 1
            except Exception as exc:
                LOG.warning('Ticket %s copy to %s failed: %s', ticket_id, target, type(exc).__name__)
        return delivered

    async def cleanup(self, ticket_id: int):
        if not self.store.ticket(ticket_id) or self.store.ticket(ticket_id)['status'] != 'closed':
            raise ValueError('Удаление переписки доступно только после закрытия тикета')
        removed=0
        failed=0
        for entry in self.store.ticket_messages(ticket_id):
            try:
                await self.bot.delete_message(entry['chat_id'],entry['message_id'])
            except TelegramBadRequest as exc:
                # "not found" and "already deleted" mean no further action is possible
                message = str(exc).lower()
                if any(key in message for key in ('not found', 'message to delete not found')):
                    self.store.ticket_deleted(ticket_id,entry['chat_id'],entry['message_id'])
                    removed+=1
                else:
                    failed+=1
            except TelegramForbiddenError:
                failed+=1
            except Exception:
                failed+=1
            else:
                self.store.ticket_deleted(ticket_id,entry['chat_id'],entry['message_id'])
                removed+=1
        return removed,failed
