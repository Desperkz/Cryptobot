"""Cheap scheduling guard for the read-only follower; never changes its cursor."""
import json
import subprocess
from pathlib import Path


def ready():
    memory = {line.split(':', 1)[0]: int(line.split()[1])
              for line in Path('/proc/meminfo').read_text().splitlines()}
    states = {name: subprocess.check_output(
        ['systemctl', 'show', name, '-p', 'ActiveState', '--value'], text=True).strip()
        for name in ('sqz-promotion-paper.service', 'sqz-runner-comparison.service',
                     'diversified-shadow.service', 'diversified-v3-shadow.service')}
    busy = any(value in ('active', 'activating', 'reloading') for value in states.values())
    minimum = (128 if busy else 80) * 1024
    allowed = memory['MemAvailable'] >= minimum and memory['SwapFree'] >= 256 * 1024
    print(json.dumps({'allowed': allowed, 'available_kib': memory['MemAvailable'],
                      'minimum_available_kib': minimum, 'other_studies': states}))
    return allowed


if __name__ == '__main__':
    raise SystemExit(0 if ready() else 1)
