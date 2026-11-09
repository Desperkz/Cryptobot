"""Synthetic exact-sandbox rename regression; never opens a production DB."""
import errno,json,os
from pathlib import Path
import subprocess,sys,time


def child(root):
    source=root/'lab0/data/state';temporary=root/'lab0/.storage-temporary/state'
    temporary.write_bytes(b'preserved');os.replace(temporary,source)
    assert source.read_bytes()==b'preserved'
    other=root/'ops/temporary/state';other.write_bytes(b'must-not-replace')
    try:os.replace(other,source)
    except OSError as exc:assert exc.errno==errno.EXDEV,repr(exc)
    else:raise AssertionError('Negative cross-bind-mount control unexpectedly succeeded')
    try:(root/'lab0/code/frozen').write_bytes(b'forbidden')
    except OSError as exc:assert exc.errno in (errno.EROFS,errno.EACCES),repr(exc)
    else:raise AssertionError('Frozen code became writable')
    assert source.read_bytes()==b'preserved'
    print(json.dumps({'ok':True,'same_lab_atomic_replace':True,
        'cross_bind_negative_control':True,'frozen_code_read_only':True}))


def main(unit):
    root=Path('/var/lib')/('research-storage-mount-probe-'+str(int(time.time()*1000)))
    for d in ('lab0/data','lab0/code','lab0/.storage-temporary','lab1/data','lab1/code',
              'ops/code','ops/data','ops/archives','ops/backups','ops/temporary','ops/reports'):
        (root/d).mkdir(parents=True,exist_ok=True)
    (root/'lab0/code/frozen').write_bytes(b'frozen')
    script=root/'probe.py';script.write_bytes(Path(__file__).read_bytes())
    props={};section=''
    allowed={'Nice','CPUQuota','MemoryHigh','MemoryMax','MemorySwapMax','TasksMax',
             'NoNewPrivileges','PrivateTmp','PrivateNetwork','RestrictAddressFamilies',
             'ProtectSystem','ProtectHome','ReadOnlyPaths','ReadWritePaths'}
    mappings={'/root/bot_research_storage':str(root/'ops'),
              '/root/bot_diversified_shadow':str(root/'lab0'),
              '/root/bot_diversified_v3':str(root/'lab1')}
    for line in Path(unit).read_text().splitlines():
        line=line.strip()
        if line.startswith('['):section=line;continue
        if section!='[Service]' or '=' not in line:continue
        k,v=line.split('=',1)
        if k not in allowed:continue
        for a,b in mappings.items():v=v.replace(a,b)
        assert '/root/' not in v,(k,v)
        props[k]=(props.get(k,'')+' '+v).strip() if k in ('ReadOnlyPaths','ReadWritePaths') else v
    command=['systemd-run','--quiet','--wait','--pipe','--collect',
             '--unit=research-storage-mount-probe-'+str(int(time.time()*1000))]
    for k,v in props.items():command+=['--property='+k+'='+v]
    command+=['/usr/bin/python3',str(script),'--child',str(root)]
    subprocess.run(command,check=True,timeout=90)


if __name__=='__main__':
    if sys.argv[1]=='--child':child(Path(sys.argv[2]))
    else:main(sys.argv[1])
