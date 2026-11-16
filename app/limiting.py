"""aiogram throttling middleware: private-user messages and callbacks."""
from __future__ import annotations
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message
from app.security import RateGate

class ThrottleMiddleware(BaseMiddleware):
    def __init__(self, admin_ids, gate=None):
        self.admin_ids=frozenset(admin_ids)
        self.gate=gate or RateGate()

    async def __call__(self, handler, event, data):
        actor=getattr(event,'from_user',None)
        # Callback data is never an authorization token. Even a forged or
        # replayed keyboard callback must come from the chat owner; admin
        # callbacks additionally require an allowlisted numeric Telegram ID.
        if isinstance(event,CallbackQuery):
            chat=getattr(getattr(event,'message',None),'chat',None)
            invalid_actor=(not actor or not chat or chat.id!=actor.id)
            admin_action=bool(event.data and event.data.startswith('a:'))
            forbidden=invalid_actor or (admin_action and actor.id not in self.admin_ids)
            if forbidden:
                try: await event.answer('Нет доступа',show_alert=True)
                except Exception: pass
                return None
        if not actor or not self.gate.allow(actor.id,actor.id in self.admin_ids):
            if isinstance(event,CallbackQuery):
                try: await event.answer('Слишком много запросов. Попробуйте позже.',show_alert=True)
                except Exception: pass
            # Never generate a reply message for every rejected message: this
            # would let spam also exhaust Telegram API limits.
            return None
        return await handler(event,data)
