"""Low-impact observer admission only; never touch trading processes."""
import shutil
from pathlib import Path

mem={line.split(':',1)[0]:int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines() if ':' in line and line.split()[1].isdigit()}
if mem.get('MemAvailable',0)<80*1024*1024 or shutil.disk_usage('/root').free<512*1024*1024:
    raise SystemExit(1)
