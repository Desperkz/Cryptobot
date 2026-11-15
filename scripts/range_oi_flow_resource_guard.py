from pathlib import Path
import shutil
data=Path('/root/bot_range_oi_flow/data')
available=int(next(x.split()[1] for x in Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemAvailable:')))
if available<64*1024:raise SystemExit('Insufficient available RAM for separate research observer')
if shutil.disk_usage(data).free<512*1024**2:raise SystemExit('Insufficient free disk for separate research observer')
if sum(p.stat().st_size for p in data.glob('range_oi_flow.sqlite3*'))>64*1024**2:raise SystemExit('Separate observer storage limit reached; history retained')
