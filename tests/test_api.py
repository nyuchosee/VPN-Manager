import json
import httpx
import pytest
from app.xui import XuiAPI, XuiError, safe_panel_message

@pytest.mark.asyncio
async def test_endpoints_and_bearer():
    requests=[]
    def handler(request:httpx.Request):
        assert request.headers['Authorization']=='Bearer test_secret'
        requests.append((request.method,request.url.path))
        if request.method=='GET' and '/get/' in request.url.path:
            return httpx.Response(200,json={'success':True,'obj':{
                'email':'someone','id':29,'uuid':'uuid-stays','subId':'sub-stays','enable':True,
                'totalGB':100,'limitHwid':3,'inboundIds':[5]}})
        if request.method=='POST' and '/update/' in request.url.path:
            body=json.loads(request.content)
            assert body['id']=='uuid-stays'
            assert 'uuid' not in body
            assert body['subId']=='sub-stays'
            assert body['limitHwid']==0
            assert 'inboundIds' not in body  # Association is not an update-body field
            return httpx.Response(200,json={'success':True})
        return httpx.Response(200,json={'success':True,'obj':[]})
    api=XuiAPI('https://example.org/panel-prefix','test_secret')
    await api.http.aclose()
    api.http=httpx.AsyncClient(transport=httpx.MockTransport(handler),headers={'Authorization':'Bearer test_secret'})
    try:
        client=await api.get('someone')
        await api.update('someone',client,{'limitHwid':0})
        await api.hwids('someone')
        await api.delete_hwid('someone',17)
        assert requests==[
            ('GET','/panel-prefix/panel/api/clients/get/someone'),
            ('POST','/panel-prefix/panel/api/clients/update/someone'),
            ('POST','/panel-prefix/panel/api/clients/hwids/someone'),
            ('DELETE','/panel-prefix/panel/api/clients/hwids/someone/17')]
    finally:await api.close()

@pytest.mark.asyncio
async def test_api_rejection_and_redirect_fail_closed():
    api=XuiAPI('https://example.org','test_secret')
    await api.http.aclose()
    api.http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(302,headers={'Location':'https://evil.example/'})),
                               headers={'Authorization':'Bearer test_secret'}, follow_redirects=False)
    try:
        with pytest.raises(XuiError):await api.clients()
    finally:await api.close()

@pytest.mark.asyncio
async def test_wrapped_client_and_multi_inbound_create():
    sent=[]
    def handler(req:httpx.Request):
        if req.method=='GET' and '/clients/get/' in req.url.path:
            return httpx.Response(200,json={'success':True,'obj':{
                'client':{'email':'alice','subId':'one','totalGB':0,'enable':True},
                'inboundIds':[1,2,4,5,6], 'externalLinks':[]}})
        if req.method=='GET' and '/inbounds/list' in req.url.path:
            return httpx.Response(200,json={'success':True,'obj':[
                {'id':1,'protocol':'vless','enable':True},
                {'id':2,'protocol':'vless','enable':True},
                {'id':4,'protocol':'trojan','enable':True}]})
        if req.method=='POST' and '/clients/add' in req.url.path:
            data=json.loads(req.content)
            sent.append(data)
            return httpx.Response(200,json={'success':True,'obj':{'client':data['client'],'inboundIds':data['inboundIds']}})
        return httpx.Response(404)
    api=XuiAPI('https://example.org/secret','token')
    await api.http.aclose()
    api.http=httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={'Authorization':'Bearer token'})
    try:
        client=await api.get('alice')
        assert client['email']=='alice' and client['inboundIds']==[1,2,4,5,6]
        assert len(await api.inbounds())==3
        await api.create({'email':'new'},(1,2,4,5,6))
        assert sent==[{'client':{'email':'new'},'inboundIds':[1,2,4,5,6]}]
    finally: await api.close()

@pytest.mark.asyncio
async def test_api_error_keeps_inbound_reason_and_redacts_secrets():
    def handler(_request):
        return httpx.Response(200,json={'success':False,
            'msg':'inbound 4: unsupported flow; password=topsecret https://example.org/sub/abc token=123'})
    api=XuiAPI('https://example.org','test_secret')
    await api.http.aclose()
    api.http=httpx.AsyncClient(transport=httpx.MockTransport(handler),headers={'Authorization':'Bearer test_secret'})
    try:
        with pytest.raises(XuiError) as err:
            await api.update('email',{'email':'email','inboundIds':[4]},{'enable':False})
        msg=str(err.value)
        assert 'inbound 4' in msg
        assert 'unsupported flow' in msg
        assert 'topsecret' not in msg
        assert 'https://' not in msg
        assert 'token=123' not in msg
    finally:
        await api.close()


def test_panel_message_sanitization():
    assert safe_panel_message(None)=='текст причины отсутствует'
    msg=safe_panel_message('inbound 2: invalid UUID 123e4567-e89b-12d3-a456-426614174000')
    assert 'inbound 2' in msg and '123e4567' not in msg

# 3x-ui v3.11.0 has TWO distinct structs for client GET vs UPDATE:
# ClientRecord.allowedIPs is a CSV string; Client.allowedIPs is []string.
# The same client update adapter is used for blocks, renewals and HWID slots.
from app.xui import update_payload


def test_update_normalizes_csv_allowed_ips_without_mutating_original():
    client = {
        'email': 'test', 'id': 8, 'uuid': 'unchanged-vless-uuid',
        'subId': 'unchanged-sub', 'inboundIds': [2, 4], 'enable': True,
        'expiryTime': 1000, 'totalGB': 500, 'limitHwid': 3,
        'allowedIPs': '10.6.0.2/32, fd00::2/128',
        'forwardedPorts': '', 'keepAlive': 0,
    }
    for changes in ({'enable': False}, {'expiryTime': 2000}, {'limitHwid': 4}):
        body = update_payload(client, changes)
        assert body['allowedIPs'] == ['10.6.0.2/32', 'fd00::2/128']
        assert body['id'] == 'unchanged-vless-uuid'
        assert body['subId'] == 'unchanged-sub'
        assert body['forwardedPorts'] == ''
        assert body['keepAlive'] == 0
        assert 'inboundIds' not in body
        assert 'uuid' not in body
        assert client['allowedIPs'] == '10.6.0.2/32, fd00::2/128'
        assert client['enable'] is True


@pytest.mark.parametrize('value', ['', None, []])
def test_empty_allowed_ips_omitted_on_normal_client(value):
    client = {'email': 'test', 'id': 9, 'uuid': 'uuid-unchanged',
              'subId': 'sub-unchanged', 'inboundIds': [2, 4],
              'allowedIPs': value, 'enable': True}
    payload = update_payload(client, {'enable': False})
    assert 'allowedIPs' not in payload
    assert payload['subId'] == 'sub-unchanged'
    assert client['allowedIPs'] == value


@pytest.mark.parametrize('invalid', [1, {}, [1], ['10.0.0.2/32', 3], '10.0.0.2/32,', ',10.0.0.2/32'])
def test_bad_allowed_ips_fail_closed(invalid):
    client = {'email':'test','id':9,'uuid':'uuid','inboundIds':[2,4],
              'allowedIPs': invalid}
    with pytest.raises(XuiError, match='allowedIPs'):
        update_payload(client, {'limitHwid': 5})


@pytest.mark.asyncio
async def test_realistic_panel_get_to_update_body_for_all_three_operations():
    sent = []
    def handler(request: httpx.Request):
        if request.method == 'GET':
            return httpx.Response(200, json={'success': True, 'obj': {
                'client': {
                    'id': 8, 'uuid': 'uuid-123', 'email': 'test',
                    'subId': 'sub-123', 'enable': True, 'expiryTime': 4000,
                    'totalGB': 500, 'limitHwid': 3, 'allowedIPs': '',
                    'forwardedPorts': '', 'keepAlive': 0,
                },
                'inboundIds': [2, 4],
            }})
        if request.method == 'POST':
            data = json.loads(request.content)
            assert 'allowedIPs' not in data   # not a string or []
            assert data['id'] == 'uuid-123'
            assert data['subId'] == 'sub-123'
            assert 'inboundIds' not in data
            sent.append(data)
            return httpx.Response(200,json={'success':True})
        raise AssertionError('Unexpected method')
    api = XuiAPI('https://example.org','token')
    await api.close()
    api.http = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={'Authorization':'Bearer token'})
    try:
        full = await api.get('test')
        for changes in ({'enable': False}, {'expiryTime': 5000, 'totalGB': 0}, {'limitHwid': 4}):
            await api.update('test', full, changes)
        assert len(sent)==3
        assert [s['enable'] for s in sent] == [False, True, True]
        assert sent[1]['expiryTime']==5000
        assert sent[2]['limitHwid']==4
    finally:
        await api.close()


def test_explicit_allowed_ips_change_is_also_normalized():
    full = {'email':'test','id':9,'uuid':'uuid','inboundIds':[2,4], 'allowedIPs':''}
    payload = update_payload(full, {'allowedIPs':'10.0.0.3/32'})
    assert payload['allowedIPs'] == ['10.0.0.3/32']
