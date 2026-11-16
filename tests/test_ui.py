"""UI behavior checked with fake Telegram transport (does not contact Telegram)."""
import asyncio
import importlib.util
import sys
import types
from pathlib import Path


def load_ui(monkeypatch):
    aiogram=types.ModuleType('aiogram')
    exceptions=types.ModuleType('aiogram.exceptions')
    classes=types.ModuleType('aiogram.types')
    class TelegramBadRequest(Exception): pass
    class TelegramForbiddenError(Exception): pass
    class Message:
        def __init__(self, chat_id, mid, sender):
            self.chat=types.SimpleNamespace(id=chat_id)
            self.message_id=mid
            self.from_user=types.SimpleNamespace(id=sender)
    class InlineKeyboardButton:
        def __init__(self, text, callback_data):
            self.text=text
            self.callback_data=callback_data
    class InlineKeyboardMarkup:
        def __init__(self,inline_keyboard):self.inline_keyboard=inline_keyboard
    exceptions.TelegramBadRequest=TelegramBadRequest
    exceptions.TelegramForbiddenError=TelegramForbiddenError
    classes.Message=Message
    classes.InlineKeyboardButton=InlineKeyboardButton
    classes.InlineKeyboardMarkup=InlineKeyboardMarkup
    monkeypatch.setitem(sys.modules,'aiogram',aiogram)
    monkeypatch.setitem(sys.modules,'aiogram.exceptions',exceptions)
    monkeypatch.setitem(sys.modules,'aiogram.types',classes)
    spec=importlib.util.spec_from_file_location('ui_isolated',Path(__file__).resolve().parents[1]/'app'/'ui.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module,Message


class FakeStore:
    def __init__(self):self.screens={}
    def ui_screen(self,chat):return self.screens.get(chat)
    def ui_set(self,chat,message):self.screens[chat]=message


class FakeBot:
    id=900
    def __init__(self):self.sent=[];self.edited=[];self.deleted=[]
    async def send_message(self,chat_id,text,reply_markup=None,protect_content=False):
        self.sent.append((chat_id,text,reply_markup,protect_content))
        return types.SimpleNamespace(message_id=100+len(self.sent))
    async def edit_message_text(self,chat_id,message_id,text,reply_markup=None):
        self.edited.append((chat_id,message_id,text,reply_markup))
        return True
    async def delete_message(self,chat_id,message_id):
        self.deleted.append((chat_id,message_id))


def test_single_screen_command_and_callback(monkeypatch):
    module,Message=load_ui(monkeypatch)
    bot=FakeBot();store=FakeStore()
    ui=module.SingleScreen(bot,store,frozenset())
    async def scenario():
        await ui.show(Message(111,1,111),'Главное меню VPN Manager')
        assert store.ui_screen(111)==101
        assert (111,1) in bot.deleted
        await ui.show(Message(111,101,900),'Моя подписка')
        assert len(bot.sent)==1 and len(bot.edited)==1
        assert bot.edited[-1][1]==101
        assert any(button.callback_data=='u:home' for row in bot.edited[-1][3].inline_keyboard for button in row)
        await ui.show(Message(111,7,111),'Главное меню VPN Manager')
        assert len(bot.sent)==1 and len(bot.edited)==2
        assert (111,7) in bot.deleted
    asyncio.run(scenario())


def test_click_notification_closes_notification_and_reuses_screen(monkeypatch):
    module,Message=load_ui(monkeypatch)
    bot=FakeBot();store=FakeStore();ui=module.SingleScreen(bot,store,frozenset({111}))
    async def scenario():
        await ui.show(Message(111,10,111),'Панель администратора.')
        await ui.show(Message(111,12,900),'Заявка №3')
        assert len(bot.sent)==1
        assert (111,12) in bot.deleted
        assert bot.edited[-1][1]==101
        assert any(button.callback_data=='a:home' for row in bot.edited[-1][3].inline_keyboard for button in row)
    asyncio.run(scenario())


def test_start_after_chat_deletion_forces_new_visible_screen(monkeypatch):
    """Old bot message may still be editable but invisible to the user."""
    module, Message = load_ui(monkeypatch)
    bot = FakeBot()
    store = FakeStore()
    ui = module.SingleScreen(bot, store, frozenset())
    store.ui_set(111, 54)  # Existing bot message in a chat user has cleared.

    async def scenario():
        await ui.show(Message(111, 77, 111), 'Главное меню VPN Manager', force_new=True)
        assert not bot.edited  # Must not edit the invisible message.
        assert len(bot.sent) == 1  # A truly new Telegram message was created.
        assert store.ui_screen(111) == 101
        assert (111, 77) in bot.deleted  # Command removed only after menu delivered.
        # Next callback must reuse the new screen, not the stale one.
        await ui.show(Message(111, 101, 900), 'Моя подписка')
        assert bot.edited[-1][1] == 101

    asyncio.run(scenario())


def test_start_send_failure_does_not_delete_command(monkeypatch):
    module, Message = load_ui(monkeypatch)
    bot = FakeBot()
    store = FakeStore()
    store.ui_set(111, 54)
    ui = module.SingleScreen(bot, store, frozenset())

    async def reject(*args, **kwargs):
        raise RuntimeError('Telegram unavailable')

    bot.send_message = reject

    async def scenario():
        try:
            await ui.show(Message(111, 77, 111), 'Главное меню VPN Manager', force_new=True)
        except RuntimeError:
            pass
        else:
            assert False, 'Send failure must propagate'
        assert not bot.deleted
        assert store.ui_screen(111) == 54

    asyncio.run(scenario())
