"""Single-screen Telegram UI. No destructive cleanup outside a private chat.

Only menu/command messages are ephemeral. Administrative events and user decisions
remain separately delivered; database audit is unaffected by Telegram cleanup.
"""
from __future__ import annotations
import asyncio
import logging
from collections import defaultdict

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

LOG = logging.getLogger('vpn-manager.ui')


class SingleScreen:
    def __init__(self, bot, store, admins):
        self.bot = bot
        self.store = store
        self.admins = admins
        self._locks = defaultdict(asyncio.Lock)

    @staticmethod
    def _add_back(text, markup, chat_id, admins):
        if 'Главное меню VPN Manager' in text or text.startswith('Панель администратора.'):
            return markup
        rows = [list(row) for row in markup.inline_keyboard] if markup else []
        values = {button.callback_data for row in rows for button in row}
        is_user = bool(values) and all(v and v.startswith('u:') for v in values)
        destination = 'u:home' if is_user or chat_id not in admins else 'a:home'
        if destination not in values:
            rows.append([InlineKeyboardButton(text='⬅️ В меню', callback_data=destination)])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def _delete(self, chat_id, message_id):
        if not message_id:
            return
        try:
            await self.bot.delete_message(chat_id, message_id)
        except (TelegramBadRequest, TelegramForbiddenError):
            pass  # Expired, absent or no permission. Do not disrupt the interaction.

    async def show(self, source: Message, text: str, reply_markup=None, *, force_new: bool = False):
        """Edit the current screen, or replace it with a new one if editing is impossible.

        The 'source' may be a user's command or a bot message attached to a
        callback. A previous bot screen is tracked in SQLite across restarts.

        Force a NEW message for /start: after a user clears their Telegram chat,
        editing a previously stored message can succeed at the Bot API level but
        remain invisible in the user's current chat. We must send a fresh screen
        before deleting the incoming /start command.
        """
        chat_id = source.chat.id
        markup = self._add_back(text, reply_markup, chat_id, self.admins)
        async with self._locks[chat_id]:
            stored = self.store.ui_screen(chat_id)
            source_id = source.message_id
            from_bot = isinstance(source, Message) and bool(source.from_user) and source.from_user.id == self.bot.id
            target = stored or (source_id if from_bot else None)
            if force_new:
                # Do not attempt to edit an invisible, stale message after the
                # user deleted their chat. Send first; delete only on success.
                new = await self.bot.send_message(chat_id, text, reply_markup=markup, protect_content=True)
                self.store.ui_set(chat_id, new.message_id)
                if target and target != new.message_id:
                    await self._delete(chat_id, target)
                if source_id != new.message_id and source_id != target:
                    await self._delete(chat_id, source_id)
                return new
            if target:
                try:
                    result = await self.bot.edit_message_text(
                        chat_id=chat_id, message_id=target, text=text, reply_markup=markup,
                    )
                    self.store.ui_set(chat_id, target)
                    if source_id != target:
                        await self._delete(chat_id, source_id)
                    return result
                except TelegramBadRequest as exc:
                    if 'message is not modified' in str(exc).lower():
                        if source_id != target:
                            await self._delete(chat_id, source_id)
                        return None
                    # The message was removed or is no longer editable.
                    LOG.debug('Cannot edit screen for chat=%s: %s', chat_id, type(exc).__name__)
            new = await self.bot.send_message(chat_id, text, reply_markup=markup, protect_content=True)
            self.store.ui_set(chat_id, new.message_id)
            if target and target != new.message_id:
                await self._delete(chat_id, target)
            if source_id != new.message_id and source_id != target:
                await self._delete(chat_id, source_id)
            return new
