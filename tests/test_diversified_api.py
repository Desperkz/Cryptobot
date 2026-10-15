import json
from pathlib import Path

import pytest
from paper_pilots_api import api_diversified_shadow

def test_missing_status_is_unavailable_and_never_creates_storage(tmp_path):
    path=tmp_path/'missing/status.json'
    result=api_diversified_shadow(path)
    assert result['health']=='UNAVAILABLE'
    assert not path.parent.exists()

def test_saved_shadow_status_is_read_only_and_stale_is_not_a_zero_result(tmp_path):
    path=tmp_path/'status.json'
    data={'generated_at_ms':100000,'mode':'SHADOW_ONLY','order_capability':False,'auto_promotion':False,'health':'OK','groups':[{'strategy':'X','net_usdt':-2}],'protocol':{}}
    path.write_text(json.dumps(data));before=path.read_bytes()
    assert api_diversified_shadow(path,now_ms=100001)['health']=='OK'
    stale=api_diversified_shadow(path,now_ms=1400000)
    assert stale['health']=='STALE' and stale['groups'][0]['net_usdt']==-2
    assert path.read_bytes()==before

@pytest.mark.parametrize('patch',[{'order_capability':True},{'auto_promotion':True},{'generated_at_ms':float('nan')},{'mode':'LIVE'}])
def test_invalid_or_order_capable_status_is_refused(tmp_path,patch):
    path=tmp_path/'status.json';data={'generated_at_ms':100000,'mode':'SHADOW_ONLY','order_capability':False,'auto_promotion':False,'health':'OK','groups':[],'protocol':{}}
    data.update(patch);path.write_text(json.dumps(data))
    assert api_diversified_shadow(path,now_ms=100001)['health']=='UNAVAILABLE'
