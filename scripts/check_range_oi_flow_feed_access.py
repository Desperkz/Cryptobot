"""Disposable Linux/systemd WAL permission regression; no production DB opens."""
import argparse,json,sqlite3,subprocess,sys,uuid
from contextlib import closing
from pathlib import Path

COPIED={'ProtectSystem','ProtectHome','PrivateTmp','NoNewPrivileges','ReadOnlyPaths','ReadWritePaths',
 'MemoryHigh','MemoryMax','MemorySwapMax','CPUQuota','TasksMax','Nice','RestrictAddressFamilies'}
def reader(root,expected):
 for name,filename in (('source','shadow.sqlite3'),('market','market_evidence.sqlite3')):
  p=root/name/filename
  with closing(sqlite3.connect(p.as_uri()+'?mode=ro',uri=True,timeout=1)) as c:
   c.execute('PRAGMA query_only=ON')
   assert [r[0] for r in c.execute('SELECT id FROM items ORDER BY id')]==expected
   c.execute('PRAGMA query_only=OFF')
   try:c.execute('INSERT INTO items VALUES(999)')
   except sqlite3.OperationalError as e:assert 'readonly' in str(e).lower()
   else:raise AssertionError('Read-only URI did not protect source')
  for protected in (p,p.with_name('status.json')):
   try:
    with protected.open('r+b'):pass
   except OSError:pass
   else:raise AssertionError('File mount not read-only:'+str(protected))
 (root/'own/data/result.json').write_text(json.dumps({'passed':True,'expected':expected,'inputs':2}))
 print(json.dumps({'passed':True,'expected':expected}))
def main(unit):
 root=Path('/var/lib')/('range-oi-flow-access-'+uuid.uuid4().hex)
 for path in ('source','market','own/data','own/code'): (root/path).mkdir(parents=True,exist_ok=True)
 writers=[]
 for name,filename in (('source','shadow.sqlite3'),('market','market_evidence.sqlite3')):
  p=root/name/filename;c=sqlite3.connect(p);c.execute('PRAGMA journal_mode=WAL')
  c.execute('CREATE TABLE items(id INTEGER PRIMARY KEY)');c.execute('INSERT INTO items VALUES(1)');c.commit()
  c.execute('INSERT INTO items VALUES(2)');writers.append(c)
  p.with_name('status.json').write_text('{}')
 helper=root/'reader.py';helper.write_bytes(Path(__file__).read_bytes())
 mappings={'/root/bot_range_oi_flow':str(root/'own'),'/root/bot_diversified_v3/data':str(root/'source'),'/root/bot_market_evidence/data':str(root/'market')}
 props={};in_service=False
 for line in unit.read_text().splitlines():
  line=line.strip()
  if line.startswith('['):in_service=line=='[Service]';continue
  if not in_service or '=' not in line:continue
  key,value=line.split('=',1)
  if key not in COPIED:continue
  for old,new in mappings.items():value=value.replace(old,new)
  if '/root/' in value:raise ValueError('Unmapped production path')
  props[key]=value
 assert {'ReadOnlyPaths','ReadWritePaths','ProtectSystem','ProtectHome'}<=props.keys()
 phases=[]
 def phase(expected):
  args=['systemd-run','--quiet','--wait','--pipe','--collect','--unit=range-oi-access-'+uuid.uuid4().hex]
  for k,v in props.items():args+=['-p',k+'='+v]
  args+=['/usr/bin/python3',str(helper),'--reader',str(root),'--expected',','.join(map(str,expected))]
  output=subprocess.check_output(args,text=True,stderr=subprocess.STDOUT,timeout=45)
  result=json.loads((root/'own/data/result.json').read_text());assert result['passed'];phases.append(result)
 try:
  phase([1])  # Live WAL: committed row visible; uncommitted row must not leak.
  for c in writers:c.rollback();c.close()
  writers.clear()
  phase([1])  # Clean-close reopen: sidecar coordination must still work.
  for name,filename in (('source','shadow.sqlite3'),('market','market_evidence.sqlite3')):
   with closing(sqlite3.connect(root/name/filename)) as c:c.execute('INSERT INTO items VALUES(2)');c.commit()
  phase([1,2])
 finally:
  for c in writers:c.close()
 print(json.dumps({'passed':True,'fixture':str(root),'phases':phases,'production_databases_opened':False}))
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--unit',type=Path);p.add_argument('--reader',type=Path);p.add_argument('--expected');a=p.parse_args()
 if a.reader:reader(a.reader,[int(x) for x in a.expected.split(',')])
 else:main(a.unit)
