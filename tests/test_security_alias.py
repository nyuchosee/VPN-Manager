"""Regression and adversarial tests for the latest display/security fixes."""
from __future__ import annotations
import json
import sqlite3
import pytest
from app.storage import Store
from app.security import RateGate, validate_alias, safe_label
from app.service import Service
from test_service import FakeAPI


def test_alias_normalization_cyrillic_and_injection_rejection():
    assert validate_alias(' Антон ') == 'Антон'
    assert validate_alias('Юридическая практика (отдел 2)') == 'Юридическая практика (отдел 2)'
    for unsafe in ['Саша\nADMIN', '\u202eAdmin', '<b>Admin</b>', "Robert'); DROP TABLE users;--", '🥷', 'a'*49, '']:
        with pytest.raises(ValueError): validate_alias(unsafe)
    assert '\u202e' not in safe_label('Лицо\u202eabc')


@pytest.mark.asyncio
async def test_alias_is_visible_but_does_not_change_3xui_credential(tmp_path):
    db=Store(str(tmp_path/'bot.db'));api=FakeAPI();service=Service(db,api,(1,2),True)
    try:
        db.upsert_user(123456,'testuser')
        rid=service.create_user_request(123456,30)
        db.set_settings(rid, alias='Антон')
        await service.approve(rid,777)
        bound=db.binding(123456)
        assert bound['display_name']=='Антон'
        assert bound['email']!='Антон'
        assert 'Антон' in [b['display_name'] for b in db.bindings()]
        preserved=dict(api.data[bound['email']])
        db.set_display_name(123456,'Пётр')
        assert db.binding(123456)['display_name']=='Пётр'
        assert api.data[bound['email']]==preserved
        assert db.db.execute('SELECT COUNT(*) FROM users').fetchone()[0]==1
        with pytest.raises(ValueError): db.set_display_name(123456,"a'; DROP TABLE users;--")
        assert db.db.execute('SELECT COUNT(*) FROM users').fetchone()[0]==1
    finally: db.close()


def test_migrate_existing_approved_alias_without_external_changes(tmp_path):
    path=str(tmp_path/'previous.sqlite3')
    # Simulate a legacy bindings table without a display_name column.
    conn=sqlite3.connect(path)
    conn.executescript('''
      CREATE TABLE users (tg_id INTEGER PRIMARY KEY, username TEXT NOT NULL DEFAULT '',
        first_name TEXT NOT NULL DEFAULT '',created_at INTEGER NOT NULL,seen_at INTEGER NOT NULL);
      CREATE TABLE bindings (tg_id INTEGER PRIMARY KEY REFERENCES users(tg_id),
        email TEXT NOT NULL UNIQUE, manual_block INTEGER NOT NULL DEFAULT 0,created_at INTEGER NOT NULL);
      CREATE TABLE requests (id INTEGER PRIMARY KEY AUTOINCREMENT,tg_id INTEGER NOT NULL REFERENCES users(tg_id),
        kind TEXT NOT NULL, requested_days INTEGER, device_id INTEGER, requested_slots INTEGER,
        status TEXT NOT NULL DEFAULT 'pending',settings TEXT NOT NULL DEFAULT '{}',result TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,reviewer INTEGER);
    ''')
    conn.execute("INSERT INTO users VALUES(123, 'test', '',1,1)")
    conn.execute("INSERT INTO bindings VALUES(123,'tg_123',0,1)")
    conn.execute("INSERT INTO requests(tg_id,kind,status,settings,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                 (123,'create','approved',json.dumps({'alias':'Антон'}),1,1))
    conn.commit();conn.close()
    db=Store(path)
    try:
        assert db.binding(123)['display_name']=='Антон'
        assert db.binding(123)['email']=='tg_123'
        assert db.db.execute('PRAGMA foreign_key_check').fetchall()==[]
    finally:db.close()
    db=Store(path)
    try:assert db.binding(123)['display_name']=='Антон'
    finally:db.close()


def test_sql_parameterization_and_injection_as_data(tmp_path):
    db=Store(str(tmp_path/'injection.db'))
    try:
        db.upsert_user(1,'normal')
        email="x'); DROP TABLE bindings;--"
        # Legacy third-party identifiers may contain punctuation, but all DB
        # operations MUST bind values through sqlite parameters.
        db.bind(1,email)
        assert db.binding_email(email)['tg_id']==1
        assert db.db.execute('SELECT COUNT(*) FROM bindings').fetchone()[0]==1
        assert db.user(1)['username']=='normal'
    finally:db.close()


def test_rate_gate_limits_traffic_and_active_id_memory():
    fake_time=[100.0]
    g=RateGate(per_user=2,admin_user=3,global_limit=5,window=60,max_users=3,
               clock=lambda:fake_time[0])
    assert g.allow(1) and g.allow(1) and not g.allow(1)
    assert g.allow(2) and g.allow(3)
    assert not g.allow(4)  # maximum tracked IDs
    fake_time[0]=161.0
    assert g.allow(4)
    assert g.allow(1,admin=True)
    assert g.allow(1,admin=True)
    assert g.allow(1,admin=True)
    assert not g.allow(1,admin=True)


@pytest.mark.asyncio
async def test_admin_list_uses_unicode_alias_not_only_technical_id(tmp_path,monkeypatch):
    pytest.importorskip('aiogram')
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app import main as bot_main
    db=Store(str(tmp_path/'display.db'))
    try:
        db.upsert_user(25,'person')
        db.bind(25,'tg_25','Антон')
        monkeypatch.setattr(bot_main,'STORE',db)
        monkeypatch.setattr(bot_main,'CFG',SimpleNamespace(admin_ids=frozenset({999})))
        render=AsyncMock()
        monkeypatch.setattr(bot_main,'view',render)
        cb=SimpleNamespace(from_user=SimpleNamespace(id=999), answer=AsyncMock(),message=object())
        await bot_main.client_list(cb)
        markup=render.await_args.kwargs['reply_markup']
        label=markup.inline_keyboard[0][0].text
        assert 'Антон' in label and 'tg_25' in label and '25' in label
    finally:db.close()


@pytest.mark.asyncio
async def test_administrative_callback_blocked_even_without_handler_check():
    pytest.importorskip('aiogram')
    from datetime import datetime, timezone
    from aiogram.types import CallbackQuery, User, Message, Chat
    from app.limiting import ThrottleMiddleware
    handler_calls=[]
    async def handler(event,data):
        handler_calls.append(event)
    user=User(id=123,is_bot=False,first_name='Visitor')
    chat=Chat(id=123,type='private')
    msg=Message(message_id=10,date=datetime.now(timezone.utc),chat=chat,text='admin')
    callback=CallbackQuery(id='test',from_user=user,chat_instance='any',message=msg,data='a:approve:777')
    guard=ThrottleMiddleware(admin_ids={999})
    await guard(handler,callback,{})
    assert not handler_calls
    # Authenticated admin clicking a different user's screen is also refused.
    admin=User(id=999,is_bot=False,first_name='Administrator')
    callback=CallbackQuery(id='test2',from_user=admin,chat_instance='any',message=msg,data='a:approve:777')
    await guard(handler,callback,{})
    assert not handler_calls


def test_persistent_ticket_quota_query_only_counts_own_chat(tmp_path):
    st=Store(str(tmp_path/'ticket.db'))
    try:
        st.upsert_user(100,'user')
        ticket=st.create_ticket(100)
        for msg_id in range(1,16):
            st.track_ticket(ticket,100,msg_id)
        for msg_id in range(1,5):
            st.track_ticket(ticket,777,msg_id)
        assert st.recent_ticket_messages(ticket,100)==15
        assert st.recent_ticket_messages(ticket,777)==4
        assert st.recent_ticket_messages(ticket,888)==0
    finally:st.close()
