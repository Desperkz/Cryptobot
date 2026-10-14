"""Cheap guard for the independent exit study; never stops another process."""
import json
import subprocess
import time
from pathlib import Path

STATUS_PATH = Path(__file__).resolve().parent.parent / 'data' / 'status.json'
MINIMUM_CYCLE_INTERVAL_MS = 300_000


def cycle_due(status_path, now_ms):
    """Retry guards cheaply, but never run the worker more often than every 5m."""
    try:
        with status_path.open('rb') as saved:
            raw = saved.read(512 * 1024 + 1)
        if len(raw) > 512 * 1024:
            return False, 'INVALID_STATUS'
        generated = json.loads(raw)['generated_at_ms']
        if isinstance(generated, bool) or not isinstance(generated, (int, float)):
            return False, 'INVALID_STATUS'
        elapsed = now_ms - int(generated)
        return elapsed >= MINIMUM_CYCLE_INTERVAL_MS, 'CYCLE_INTERVAL'
    except FileNotFoundError:
        return True, 'NO_STATUS_YET'
    except (ValueError, TypeError, KeyError, OverflowError):
        return False, 'INVALID_STATUS'


def ready(status_path=STATUS_PATH, now_ms=None):
    memory={line.split(':',1)[0]:int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()}
    promotion=subprocess.check_output(['systemctl','show','sqz-promotion-paper.service','-p','ActiveState','--value'],text=True).strip()
    due, reason = cycle_due(status_path, int(time.time()*1000) if now_ms is None else now_ms)
    allowed=memory['MemAvailable']>=64*1024 and memory['SwapFree']>=256*1024 and promotion=='inactive' and due
    print(json.dumps({'guard':'READY' if allowed else 'DEFERRED','available_kib':memory['MemAvailable'],
        'swap_free_kib':memory['SwapFree'],'promotion_state':promotion,
        'cycle_due':due,'schedule_reason':reason}))
    return allowed


if __name__=='__main__':raise SystemExit(0 if ready() else 1)
