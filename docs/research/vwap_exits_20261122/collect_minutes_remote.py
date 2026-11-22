"""Download only public 1m klines for the 26 frozen 8h input windows."""
import gzip
import hashlib
import json
import math
from pathlib import Path
import time
import urllib.parse
import urllib.request

BASE=Path('/root/vwap_exit_research_20261122')
URL='https://fapi.binance.com/fapi/v1/klines'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args):raise ValueError('REDIRECT_REFUSED')


def main():
    info={line.split(':')[0]:int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')}
    if info['MemAvailable']<81920:raise ValueError('AVAILABLE_MEMORY_BELOW_80MIB')
    raw=(BASE/'input_plans.json').read_bytes();inputs=json.loads(raw)
    assert len(inputs['plans'])==26 and len({p['id'] for p in inputs['plans']})==26
    output={'source_plans_sha256':hashlib.sha256(raw).hexdigest(),'declaration_sha256':inputs['declaration_sha256'],
            'endpoint':URL,'started_ms':int(time.time()*1000),'windows':[]}
    seen={}
    for p in inputs['plans']:
        assert p['strategy']=='VWAP_REVERSION_V3_RECLAIM' and p['expires_ms']-p['opened_ms']==28800000
        start=(p['opened_ms']//60000+1)*60000;end=(p['expires_ms']//60000+1)*60000-1
        params={'symbol':p['symbol'],'interval':'1m','startTime':start,'endTime':end,'limit':500}
        request=urllib.request.Request(URL+'?'+urllib.parse.urlencode(params),headers={'User-Agent':'vwap-exits-20261122'})
        for attempt in range(4):
            try:
                with urllib.request.build_opener(NoRedirect()).open(request,timeout=8) as response:payload=response.read(512*1024+1)
                break
            except OSError:
                if attempt==3:raise
                time.sleep(1+attempt)
        assert len(payload)<=512*1024
        rows=json.loads(payload);assert len(rows)==480
        for i,b in enumerate(rows):
            assert isinstance(b,list) and len(b)>=7
            assert b[0]==start+i*60000 and b[6]==b[0]+59999 and b[6]<=end
            o,h,l,c,v=map(float,b[1:6]);assert all(math.isfinite(x) for x in (o,h,l,c,v)) and 0<l<=min(o,c)<=max(o,c)<=h and v>=0
            key=(p['symbol'],b[0]);value=json.dumps(b,separators=(',',':'))
            assert key not in seen or seen[key]==value,'OVERLAPPING_MINUTE_CHANGED'
            seen[key]=value
        output['windows'].append({'source_id':p['id'],'symbol':p['symbol'],'request':params,
                                  'received_ms':int(time.time()*1000),'response_sha256':hashlib.sha256(payload).hexdigest(),'bars':rows})
        print(json.dumps({'downloaded':len(output['windows']),'source_id':p['id'],'rows':len(rows)}),flush=True)
        time.sleep(.2)
    output['finished_ms']=int(time.time()*1000)
    packed=gzip.compress((json.dumps(output,separators=(',',':'),allow_nan=False)+'\n').encode(),mtime=0)
    assert not (BASE/'minutes.json.gz').exists()
    (BASE/'minutes.json.gz').write_bytes(packed)
    print(json.dumps({'windows':len(output['windows']),'unique_minutes':len(seen),'bytes':len(packed),'sha256':hashlib.sha256(packed).hexdigest()}))


if __name__=='__main__':main()
