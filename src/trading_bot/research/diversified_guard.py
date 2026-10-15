"""Low-cost resource/scheduling guard for the isolated diversified lab."""
import json
from pathlib import Path
import subprocess
import time

def ready(data_dir=Path('/root/bot_diversified_shadow/data'), now_ms=None):
    now=int(time.time()*1000) if now_ms is None else now_ms
    memory={s.split(':',1)[0]:int(s.split()[1]) for s in Path('/proc/meminfo').read_text().splitlines()}
    states={n:subprocess.check_output(['systemctl','show',n,'-p','ActiveState','--value'],text=True).strip() for n in ('sqz-promotion-paper.service','sqz-runner-comparison.service')}
    try:
        saved=json.loads((data_dir/'status.json').read_text());stamp=saved['generated_at_ms']
        due=not isinstance(stamp,bool) and isinstance(stamp,int) and now-stamp>=300000
    except FileNotFoundError:due=True
    except (KeyError,ValueError,TypeError):due=False
    allowed=due and memory['MemAvailable']>=80*1024 and memory['SwapFree']>=256*1024 and all(v=='inactive' for v in states.values())
    print(json.dumps({'allowed':allowed,'cycle_due':due,'available_kib':memory['MemAvailable'],'other_studies':states}))
    return allowed

if __name__=='__main__':raise SystemExit(0 if ready() else 1)
