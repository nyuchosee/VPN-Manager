import re
from app.domain import DAY_MS,GIB,new_expiry,gb_bytes,validate_hwid,client_name,remaining,status
from app.xui import update_payload,XuiError
import pytest

def test_expiry_active_expired_unlimited():
    now=2_000_000_000_000
    assert new_expiry(now+5*DAY_MS,30,now)==now+35*DAY_MS
    assert new_expiry(now-5*DAY_MS,30,now)==now+30*DAY_MS
    assert new_expiry(0,None,now)==0
    assert new_expiry(0,7,now)==now+7*DAY_MS

def test_limits():
    assert gb_bytes(500)==500*GIB
    assert gb_bytes(None)==0
    assert validate_hwid(None)==0
    assert validate_hwid(3)==3
    for val in (0,-1,1.5,36501):
        with pytest.raises(ValueError):new_expiry(0,val,0)

def test_names():
    assert client_name('alex_ivanov',42,set())=='alex_ivanov'
    assert client_name(None,42,set())=='tg_42'
    assert client_name('alex',42,{'alex'}).startswith('alex_')
    assert re.fullmatch(r'[A-Za-z0-9_.-]+',client_name('hello world',42,set()))

def test_update_full_fails_closed():
    c={'email':'a','uuid':'secret','subId':'fixed','totalGB':20,'limitHwid':3,'enable':True,'traffic':{'up':10}}
    result=update_payload(c,{'totalGB':30})
    assert result['id']=='secret' and result['subId']=='fixed' and result['enable'] is True
    assert 'uuid' not in result
    assert 'traffic' not in result and result['totalGB']==30
    with pytest.raises(XuiError):update_payload({**c,'new_sensitive_field':42},{'totalGB':30})

def test_status():
    assert status({'expiryTime':0,'enable':True,'totalGB':0})=='Активна'
    assert status({'expiryTime':0,'enable':False})=='Отключена'
    assert status({'expiryTime':0,'enable':True,'totalGB':100,'traffic':{'up':40,'down':60}})=='Трафик исчерпан'


def test_3xui_311_extended_client_fields_round_trip():
    # Screenshot-derived fields also belong to the 3x-ui client schema;
    # treating them as unknown would block the read-only compatibility probe.
    original = {
        'email': 'alice', 'subId': 'fixed', 'uuid': 'uuid-fixed',
        'expiryTime': 123, 'totalGB': 2048, 'limitHwid': 3,
        'inboundIds': [2, 4], 'traffic': {'up': 42, 'down': 84},
        'allowedIPs': ['10.10.0.2/32'],
        'forwardedPorts': '', 'keepAlive': 25,
        'preSharedKey': 'preshared-secret',
        'privateKey': 'private-secret', 'publicKey': 'public-key',
    }
    from copy import deepcopy
    untouched = deepcopy(original)
    result = update_payload(original, {'expiryTime': 456})
    assert original == untouched  # Never mutate API readback.
    for key in ('allowedIPs', 'forwardedPorts', 'keepAlive', 'preSharedKey', 'privateKey', 'publicKey'):
        assert result[key] == original[key]
    assert result['expiryTime'] == 456
    assert result['subId'] == 'fixed' and result['id'] == 'uuid-fixed'
    assert 'uuid' not in result
    assert 'inboundIds' not in result  # read-only attachment, not a client update field
    assert 'traffic' not in result  # Server-maintained stats are not editable.

def test_3xui_311_still_fails_closed_on_future_fields_and_bad_changes():
    valid = {'email':'alice', 'subId':'fixed', 'allowedIPs':''}
    with pytest.raises(XuiError, match='неизвестные поля'):
        update_payload({**valid, 'unexpectedNewField': 7}, {})
    with pytest.raises(XuiError, match='Недопустимые поля'):
        update_payload(valid, {'traffic': {'up': 0}})
    with pytest.raises(XuiError, match='Недопустимые поля'):
        update_payload(valid, {'unexpectedNewField': 7})


def test_get_numeric_id_mapped_to_uuid_not_pk():
    original = {'email':'alice','id':17,'uuid':'123e4567-e89b-42d3-a456-426614174000',
                'subId':'stable','totalGB':500,'enable':True,'inboundIds':[2,4]}
    output=update_payload(original, {'enable':False})
    assert output['id']==original['uuid']
    assert output['id'] != '17'
    assert output['enable'] is False
    assert output['subId']=='stable'
    assert 'uuid' not in output and 'inboundIds' not in output
    assert original['id']==17


def test_no_numeric_primary_key_without_uuid_sent():
    with pytest.raises(XuiError,match='без UUID'):
        update_payload({'email':'alice','id':17,'uuid':'','subId':'ok'},{'enable':False})


def test_identity_fields_rejected_as_changes():
    original={'email':'alice','id':17,'uuid':'valid-uuid','subId':'stable'}
    for field in ('email','id','uuid','subId'):
        with pytest.raises(XuiError, match='Недопустимые поля'):
            update_payload(original,{field:'different'})
