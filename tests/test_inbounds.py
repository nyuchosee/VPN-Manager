import pytest
from app.config import parse_inbound_ids
from app.inbounds import validate_selected_inbounds


def test_parse_multiple_ids():
    assert parse_inbound_ids('1, 2,4, 5,6') == (1,2,4,5,6)
    for bad in ('', '1,1','0,1','1,,2','1 2','1, -2', '1;2'):
        with pytest.raises(ValueError):parse_inbound_ids(bad)


def test_validate_selection():
    inbounds=[{'id':1,'enable':True,'protocol':'vless','remark':'reality'},
              {'id':2,'enable':True,'protocol':'vless','remark':'ws'},
              {'id':3,'enable':False,'protocol':'vless','remark':'xhttp'},
              {'id':4,'enable':True,'protocol':'trojan','remark':'grpc'}]
    assert [i['id'] for i in validate_selected_inbounds((1,2,4),inbounds)] == [1,2,4]
    with pytest.raises(ValueError,match='выключены'):validate_selected_inbounds((1,3),inbounds)
    with pytest.raises(ValueError,match='отсутствуют'):validate_selected_inbounds((1,5),inbounds)
