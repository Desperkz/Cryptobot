from pathlib import Path
import shutil
data=Path('/root/bot_market_evidence/data')
values={line.split(':')[0]:int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')}
if values['MemAvailable']<80*1024:raise SystemExit('Not enough available RAM for separate research collector')
if shutil.disk_usage(data).free<1024**3:raise SystemExit('Not enough free disk for separate research collector')
