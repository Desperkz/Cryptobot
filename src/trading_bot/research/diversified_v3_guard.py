"""Bounded scheduling guard; does not modify source or production services."""
import json
from pathlib import Path
import subprocess


def ready():
    memory={line.split(':',1)[0]:int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()}
    states={n:subprocess.check_output(['systemctl','show',n,'-p','ActiveState','--value'],text=True).strip()
        for n in ('sqz-promotion-paper.service','sqz-runner-comparison.service','diversified-shadow.service')}
    # This is a bounded read-only snapshot follower, not another expensive scan.
    # Blanket serialization lost the 120s freshness window behind long pilots.
    busy=any(v in ('active','activating','reloading') for v in states.values())
    minimum=(128 if busy else 80)*1024
    allowed=memory['MemAvailable']>=minimum and memory['SwapFree']>=256*1024
    print(json.dumps({'allowed':allowed,'available_kib':memory['MemAvailable'],
        'minimum_available_kib':minimum,'other_studies':states}))
    return allowed


if __name__=='__main__':raise SystemExit(0 if ready() else 1)
