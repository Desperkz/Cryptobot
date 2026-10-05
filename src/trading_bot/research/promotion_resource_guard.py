"""Cheap systemd ExecCondition before importing the research runtime."""
import json
from pathlib import Path

if __name__=='__main__':
    m={s.split(':',1)[0]:int(s.split()[1]) for s in Path('/proc/meminfo').read_text().splitlines()}
    ready=m['MemAvailable']>=64*1024 and m['SwapFree']>=256*1024
    print(json.dumps({'resource_guard':'READY' if ready else 'DEFERRED',
                      'available_memory_kib':m['MemAvailable'],'free_swap_kib':m['SwapFree']}))
    raise SystemExit(0 if ready else 1)
