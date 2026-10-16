"""Reproduce an isolated trade audit without exchange access or live writes."""
import argparse
import gzip
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from trading_bot.research.trade_audit import analyze

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--data-dir',type=Path,required=True,help='evidence.json.gz and SYMBOL_1m.json.gz')
parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args()
evidence=json.loads(gzip.decompress((args.data_dir/'evidence.json.gz').read_bytes()))
minutes={s:json.loads(gzip.decompress((args.data_dir/(s+'_1m.json.gz')).read_bytes()))
    for s in {p['symbol'] for p in evidence['positions']}}
report=analyze(evidence,minutes)
args.output.write_text(json.dumps(report,indent=2,allow_nan=False),encoding='utf-8')
print(json.dumps({'closed_replayed':sum(g['closed'] for g in report['groups'].values()),
    'vwap':report['groups'].get('VWAP_REVERSION_V2'),'v3_diagnostic':report['vwap_v3_subset']}))
