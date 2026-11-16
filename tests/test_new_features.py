import sqlite3
from pathlib import Path
import pytest
from app.storage import Store
from app.domain import DAY_MS, GIB, now_ms, replace_expiry, exact_expiry
from app.service import Service
from app.tickets import TicketRelay
from test_service import FakeAPI


def test_replace_duration_and_date():
    base=1_800_000_000_000
    assert replace_expiry(7,base)==base+7*DAY_MS
    assert replace_expiry(None,base)==0
    assert exact_expiry('2028-01-01',base)==exact_expiry('2028-01-01 23:59',base)
    with pytest.raises(ValueError):exact_expiry('2026-01-01 00:00',base)
    with pytest.raises(ValueError):exact_expiry('01.01.2028',base)


@pytest.mark.asyncio
async def test_renew_from_now_no_additive_and_unlimited_change(tmp_path):
    st=Store(str(tmp_path/'bot.db'));api=FakeAPI();svc=Service(st,api,(1,2),True)
    try:
        st.upsert_user(1,'a');await svc.approve(svc.create_user_request(1,90),9)
        before=api.data['a']['expiryTime']
        await svc.approve(svc.renew_request(1,7),9)
        assert now_ms()+6*DAY_MS <= api.data['a']['expiryTime'] <= now_ms()+7*DAY_MS+10000
        assert api.data['a']['expiryTime']<before
        req=svc.admin_request(1,'adjust',{'days':None})
        await svc.approve(req,9)
        assert api.data['a']['expiryTime']==0
        req=svc.renew_request(1,30)
        with pytest.raises(ValueError,match='явно'):await svc.approve(req,9)
        st.set_settings(req,explicit_from_unlimited=True)
        await svc.approve(req,9)
        assert now_ms()+29*DAY_MS <= api.data['a']['expiryTime'] <= now_ms()+30*DAY_MS+10000
        req=svc.admin_request(1,'adjust',{'expiry_at':exact_expiry('2028-01-01')})
        await svc.approve(req,9)
        assert api.data['a']['expiryTime']==exact_expiry('2028-01-01')
    finally: st.close()


@pytest.mark.asyncio
async def test_delete_requires_confirmation_and_preserves_other_clients(tmp_path):
    st=Store(str(tmp_path/'bot.db'));api=FakeAPI();svc=Service(st,api,(1,2),True)
    try:
        for uid,n in ((1,'a'),(2,'b')):
            st.upsert_user(uid,n);await svc.approve(svc.create_user_request(uid,7),9)
        rid=svc.admin_request(1,'delete')
        with pytest.raises(ValueError,match='подтверждения'):await svc.approve(rid,9)
        st.set_settings(rid,delete_confirm=True)
        await svc.approve(rid,9)
        assert 'a' not in api.data and 'b' in api.data
        assert st.binding(1) is None and st.binding(2)
        assert st.request(rid)['status']=='approved'
        assert st.operation(rid)['stage']=='complete'
    finally:st.close()


@pytest.mark.asyncio
async def test_delete_refuses_other_open_requests(tmp_path):
    st=Store(str(tmp_path/'bot.db'));api=FakeAPI();svc=Service(st,api,(1,2),True)
    try:
        st.upsert_user(1,'a');await svc.approve(svc.create_user_request(1,7),9)
        other=svc.slots_request(1,1)
        rid=svc.admin_request(1,'delete',{'delete_confirm':True})
        with pytest.raises(ValueError,match='другие открытые'):await svc.approve(rid,9)
        assert 'a' in api.data
    finally:st.close()


def test_migrate_previous_db_preserves_fks_and_request_sequence(tmp_path):
    path=str(tmp_path/'old.db')
    st=Store(path)
    st.upsert_user(1,'a')
    rid=st.create_request(1,'renew',days=7)
    st.transition(rid,'pending','processing')
    st.add_operation(rid,'renew','a',{'dummy':True})
    st.close()
    db=sqlite3.connect(path)
    sql=db.execute("SELECT sql FROM sqlite_master WHERE name='requests'").fetchone()[0]
    db.execute('PRAGMA foreign_keys=OFF')
    # Simulate legacy schema (same columns, older CHECK constraint)
    db.execute('BEGIN')
    db.execute(sql.replace('requests (','requests_legacy (').replace(",'delete'",''))
    db.execute('INSERT INTO requests_legacy SELECT * FROM requests')
    db.execute('DROP TABLE requests')
    db.execute('ALTER TABLE requests_legacy RENAME TO requests')
    db.execute('COMMIT')
    db.close()
    reopened=Store(path)
    try:
        assert reopened.request(rid)['kind']=='renew'
        assert reopened.operation(rid)['stage']=='planned'
        assert not reopened.db.execute('PRAGMA foreign_key_check').fetchall()
        next_id=reopened.create_request(1,'delete')
        assert next_id>rid
    finally:reopened.close()


class FakeMsg:
    def __init__(self,mid):self.message_id=mid

class FakeBot:
    def __init__(self):self.out={};self.deleted=[];self.next=100
    async def send_message(self,chat_id,text,**kwargs):
        self.next+=1; self.out[(chat_id,self.next)]=text
        return FakeMsg(self.next)
    async def copy_message(self,chat_id,from_chat_id,message_id):
        self.next+=1;self.out[(chat_id,self.next)]=f'copy:{from_chat_id}/{message_id}'
        return FakeMsg(self.next)
    async def delete_message(self,chat_id,message_id):
        if message_id==7:
            raise Exception('message cannot be deleted')
        self.deleted.append((chat_id,message_id))


@pytest.mark.asyncio
async def test_ticket_messages_both_sides_cleanup_and_retry(tmp_path):
    st=Store(str(tmp_path/'bot.db'));bot=FakeBot()
    try:
        relay=TicketRelay(bot,st,{20,21})
        st.upsert_user(10,'a')
        tid=st.create_ticket(10)
        assert st.create_ticket(10)==tid
        await relay.notify_admins(tid,'Новый тикет')
        class Chat: id=10
        class In: chat=Chat();message_id=7
        await relay.relay(tid,In(),[20,21])
        assert st.pending_ticket_cleanup(tid)==7
        assert st.close_ticket(tid,10)
        assert not st.close_ticket(tid,10)
        removed,failed=await relay.cleanup(tid)
        assert removed==6 and failed==1
        assert st.pending_ticket_cleanup(tid)==1
        removed2,failed2=await relay.cleanup(tid)
        assert removed2==0 and failed2==1
        assert st.create_ticket(10)!=tid
    finally:st.close()

@pytest.mark.asyncio
async def test_delete_api_error_keeps_binding_and_requires_review(tmp_path):
    class FailedDelete(FakeAPI):
        async def delete_client(self,email):
            raise RuntimeError('inbound 4: operation rejected')
    st=Store(str(tmp_path/'bot.db'));api=FailedDelete();svc=Service(st,api,(1,2),True)
    try:
        st.upsert_user(1,'example')
        await svc.approve(svc.create_user_request(1,7),9)
        rid=svc.admin_request(1,'delete',{'delete_confirm':True})
        with pytest.raises(ValueError,match='требует проверки'):
            await svc.approve(rid,9)
        assert st.binding(1)['email']=='example'
        assert st.request(rid)['status']=='needs_review'
        assert st.operation(rid)['last_progress_stage']=='delete_started'
        assert 'example' in api.data
    finally:st.close()


def test_ticket_screen_reassociation_does_not_delete_other_ticket_ui(tmp_path):
    st=Store(str(tmp_path/'bot.db'))
    try:
        st.upsert_user(10);st.upsert_user(11)
        a=st.create_ticket(10);b=st.create_ticket(11)
        st.track_ticket(a,20,777)
        st.track_ticket(b,20,777)
        assert st.ticket_messages(a)==[]
        assert len(st.ticket_messages(b))==1
    finally:st.close()


def test_date_settings_override_existing_day_selection(tmp_path):
    st=Store(str(tmp_path/'bot.db'))
    try:
        st.upsert_user(10)
        rid=st.create_request(10,'renew',days=30)
        st.set_settings(rid,days=90)
        st.set_settings(rid,expiry_at=exact_expiry('2028-10-15 17:30'))
        st.remove_settings(rid,'days')
        assert 'days' not in st.settings(st.request(rid))
        st.set_settings(rid,days=7)
        st.remove_settings(rid,'expiry_at')
        assert 'expiry_at' not in st.settings(st.request(rid))
    finally:st.close()
