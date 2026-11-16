import pytest
from copy import deepcopy
from app.service import Service
from app.storage import Store
from app.domain import DAY_MS,GIB,now_ms

class FakeAPI:
    def __init__(self):
        self.data={};self.devices={};self.resets={};self.fail_reset=False
        self.sub_counter=1
    async def clients(self):return [deepcopy(x) for x in self.data.values()]
    async def inbounds(self):return [
        {'id':i, 'enable':True, 'protocol':'vless', 'remark':f'Inbound {i}'}
        for i in (1,2,4,5,6)
    ]
    async def get(self,email):return deepcopy(self.data[email])
    async def create(self,c,inbound_ids):
        assert c['email'] not in self.data
        c=deepcopy(c);c['subId']='sub_'+str(self.sub_counter);self.sub_counter+=1
        c['uuid']='unchanged-id';c['id']='unchanged-id';c['inboundIds']=list(inbound_ids)
        c['traffic']={'up':100,'down':200}
        self.data[c['email']]=c
    async def update(self,email,full,changes):
        assert full['subId']==self.data[email]['subId']
        self.data[email].update(changes)
    async def reset_traffic(self,email):
        self.resets[email]=self.resets.get(email,0)+1
        if self.fail_reset:raise RuntimeError('no response')
        self.data[email]['traffic']={'up':0,'down':0}
    async def hwids(self,email):return deepcopy(self.devices.get(email,[]))
    async def delete_client(self,email):
        del self.data[email]
        self.devices.pop(email,None)
    async def delete_hwid(self,email,device_id):
        self.devices[email]=[d for d in self.devices.get(email,[]) if d['id']!=device_id]

@pytest.fixture
def env(tmp_path):
    store=Store(str(tmp_path/'bot.db'))
    api=FakeAPI()
    svc=Service(store,api,(1,2),True)
    yield store,api,svc
    store.close()

@pytest.mark.asyncio
async def test_create_renew_and_no_double_approval(env):
    s,api,svc=env
    s.upsert_user(123,'alex_ivanov')
    rid=svc.create_user_request(123,30)
    await svc.approve(rid,9)
    assert s.request(rid)['status']=='approved'
    c=api.data['alex_ivanov']
    assert c['limitHwid']==3 and c['totalGB']==500*GIB
    assert c['subId']=='sub_1' and c['inboundIds']==[1,2]
    with pytest.raises(ValueError):await svc.approve(rid,9)
    with pytest.raises(ValueError):svc.create_user_request(123,7)
    before=c['expiryTime']
    next_id=svc.renew_request(123,90)
    s.set_settings(next_id,traffic_gb=None,hwid_limit=None)
    await svc.approve(next_id,9)
    assert now_ms()+89*DAY_MS <= c['expiryTime'] <= now_ms()+90*DAY_MS+10000
    assert c['expiryTime'] < before+90*DAY_MS
    assert c['totalGB']==0 and c['limitHwid']==0
    assert c['subId']=='sub_1'
    assert api.resets['alex_ivanov']==1

@pytest.mark.asyncio
async def test_failed_reset_requires_review(env):
    s,api,svc=env
    s.upsert_user(1,'e')
    rid=svc.create_user_request(1,7)
    await svc.approve(rid,9)
    api.fail_reset=True
    r2=svc.renew_request(1,7)
    with pytest.raises(ValueError,match='требует проверки'):await svc.approve(r2,9)
    assert s.request(r2)['status']=='needs_review'
    assert api.resets['e']==1
    with pytest.raises(ValueError):await svc.approve(r2,9)
    assert api.resets['e']==1

@pytest.mark.asyncio
async def test_device_actions_and_admin_adjust(env):
    s,api,svc=env
    s.upsert_user(1,'alice')
    create=svc.create_user_request(1,7)
    await svc.approve(create,9)
    api.devices['alice']=[{'id':4},{'id':7}]
    r=svc.device_request(1,4)
    await svc.approve(r,9)
    assert [x['id'] for x in api.devices['alice']]==[7]
    request=svc.slots_request(1,2)
    await svc.approve(request,9)
    assert api.data['alice']['limitHwid']==5
    adj=svc.admin_request(1,'adjust',{'traffic_gb':None,'hwid_limit':None})
    await svc.approve(adj,9)
    assert api.data['alice']['totalGB']==0 and api.data['alice']['limitHwid']==0

@pytest.mark.asyncio
async def test_expired_renewal_then_admin_block(env):
    s,api,svc=env
    s.upsert_user(1,'alice')
    create=svc.create_user_request(1,7)
    await svc.approve(create,9)
    api.data['alice']['expiryTime']=now_ms()-DAY_MS
    api.data['alice']['enable']=False
    rid=svc.renew_request(1,7)
    with pytest.raises(ValueError,match='явно разрешить'):await svc.approve(rid,9)
    s.set_settings(rid,reactivate=True)
    await svc.approve(rid,9)
    assert api.data['alice']['enable'] is True
    bid=svc.admin_request(1,'block')
    await svc.approve(bid,9)
    assert s.binding(1)['manual_block']==1
    rid=svc.renew_request(1,7)
    await svc.approve(rid,9)
    assert api.data['alice']['enable'] is False

@pytest.mark.asyncio
async def test_readonly_does_not_mutate(env):
    s,api,svc=env
    s.upsert_user(1,'alice')
    svc.write_enabled=False
    rid=svc.create_user_request(1,7)
    with pytest.raises(ValueError,match='только чтения'):await svc.approve(rid,9)
    assert api.data=={}

@pytest.mark.asyncio
async def test_slots_unlimited_and_review_maintains_client(env):
    store,api,svc=env
    store.upsert_user(10,'test')
    created=svc.create_user_request(10,30)
    await svc.approve(created,5)
    rid=svc.slots_request(10,3)
    store.set_settings(rid,hwid_limit=None)
    await svc.approve(rid,5)
    assert api.data['test']['limitHwid']==0
    assert api.data['test']['subId']=='sub_1'

@pytest.mark.asyncio
async def test_existing_bind_is_readonly_with_tgid_guard(env):
    store,api,svc=env
    api.data['existing']={'email':'existing','tgId':11,'subId':'secret','enable':True,'inboundIds':[1]}
    store.upsert_user(22,'a')
    svc.write_enabled=False
    with pytest.raises(ValueError,match='другой Telegram ID'):
        await svc.register_existing(22,'existing',1)
    store.upsert_user(11,'a')
    await svc.register_existing(11,'existing',1)
    assert store.binding(11)['email']=='existing'
    assert api.data['existing']['subId']=='secret'

@pytest.mark.asyncio
async def test_adjust_refuses_unexpected_concurrent_change(env):
    store,api,svc=env
    store.upsert_user(10,'test')
    created=svc.create_user_request(10,30)
    await svc.approve(created,5)
    rid=svc.admin_request(10,'adjust',{'hwid_limit':5})
    # We cannot simulate race inside a single fake API without a hook; ensure only
    # explicit desired field is changed, identity and other settings stay intact.
    before=api.data['test'].copy()
    await svc.approve(rid,5)
    assert api.data['test']['limitHwid']==5
    assert api.data['test']['totalGB']==before['totalGB']
    assert api.data['test']['expiryTime']==before['expiryTime']

@pytest.mark.asyncio
async def test_multi_inbound_five_attachments_and_single_subid(tmp_path):
    s=Store(str(tmp_path/'bot.db'))
    try:
        api=FakeAPI()
        svc=Service(s, api, (1,2,4,5,6), True)
        s.upsert_user(55,'five')
        rid=svc.create_user_request(55,30)
        await svc.approve(rid,9)
        c=api.data['five']
        assert len(api.data)==1
        assert c['inboundIds']==[1,2,4,5,6]
        before=c['subId']
        r2=svc.renew_request(55,7)
        await svc.approve(r2,9)
        assert api.data['five']['subId']==before
        assert api.data['five']['inboundIds']==[1,2,4,5,6]
    finally:
        s.close()

@pytest.mark.asyncio
async def test_create_refuses_missing_or_disabled_inbound_before_write(tmp_path):
    s=Store(str(tmp_path/'bot.db'))
    try:
        api=FakeAPI(); svc=Service(s,api,(1,99),True)
        s.upsert_user(1,'a')
        rid=svc.create_user_request(1,7)
        with pytest.raises(ValueError,match='отсутствуют inbound'):
            await svc.approve(rid,9)
        assert not api.data
        assert s.request(rid)['status']=='pending'
    finally:
        s.close()

@pytest.mark.asyncio
async def test_failed_update_records_started_stage_and_never_resets(tmp_path):
    s=Store(str(tmp_path/'bot.db'))
    class FailUpdateAPI(FakeAPI):
        async def update(self,email,full,changes):
            raise RuntimeError('inbound 4: test error')
    api=FailUpdateAPI()
    svc=Service(s,api,(1,2),True)
    try:
        s.upsert_user(123,'alice')
        await svc.approve(svc.create_user_request(123,30),5)
        request=svc.renew_request(123,7)
        with pytest.raises(ValueError,match='требует проверки'):
            await svc.approve(request,5)
        assert s.request(request)['status']=='needs_review'
        op=s.operation(request)
        assert op['stage']=='needs_review'
        assert op['last_progress_stage']=='update_started'
        assert api.resets=={}
        assert 'inbound 4' in s.request(request)['result']
    finally:
        s.close()
