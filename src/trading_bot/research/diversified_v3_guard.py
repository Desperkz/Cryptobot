"""Bounded scheduling guard; does not modify source or production services."""
import json
from pathlib import Path
import subprocess


def ready():
    memory={line.split(':',1)[0]:int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()}
    states={n:subprocess.check_output(['systemctl','show',n,'-p','ActiveState','--value'],text=True).strip()
        for n in ('sqz-promotion-paper.service','sqz-runner-comparison.service','diversified-shadow.service')}
    allowed=memory['MemAvailable']>=80*1024 and memory['SwapFree']>=256*1024 and all(v=='inactive' for v in states.values())
    print(json.dumps({'allowed':allowed,'available_kib':memory['MemAvailable'],'other_studies':states}))
    return allowed


if __name__=='__main__':raise SystemExit(0 if ready() else 1)
