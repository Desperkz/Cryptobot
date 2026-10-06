"""Cheap guard for the independent exit study; never stops another process."""
import json
import subprocess
from pathlib import Path


def ready():
    memory={line.split(':',1)[0]:int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()}
    promotion=subprocess.check_output(['systemctl','show','sqz-promotion-paper.service','-p','ActiveState','--value'],text=True).strip()
    allowed=memory['MemAvailable']>=64*1024 and memory['SwapFree']>=256*1024 and promotion=='inactive'
    print(json.dumps({'guard':'READY' if allowed else 'DEFERRED','available_kib':memory['MemAvailable'],
        'swap_free_kib':memory['SwapFree'],'promotion_state':promotion}))
    return allowed


if __name__=='__main__':raise SystemExit(0 if ready() else 1)
