"""Read-only, consistent observer summary. No PnL or independent-trade claims."""
import argparse
import hashlib
import json
import sqlite3
import zlib
from collections import Counter
from pathlib import Path


def report(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        db.execute('BEGIN')
        meta = {k: json.loads(v) for k, v in db.execute('SELECT key,value FROM meta')}
        coverage = Counter()
        for (value,) in db.execute('SELECT payload FROM coverage'):
            coverage[json.loads(value)['status']] += 1
        counts, liquidity, early, symbols = Counter(), Counter(), Counter(), Counter()
        sources = []
        # Use only the first recorded observation per source for refusal counts.
        for source, observed, sha, blob in db.execute('''SELECT o.source_id,o.observed_ms,
                o.payload_sha256,o.payload_zlib FROM observations o
                JOIN (SELECT source_id,min(id) AS first_id FROM observations GROUP BY source_id) f
                ON o.id=f.first_id ORDER BY o.id'''):
            raw = zlib.decompress(blob)
            if hashlib.sha256(raw).hexdigest() != sha:
                raise ValueError('Evidence checksum mismatch: ' + source)
            p = json.loads(raw)
            failures = p.get('liquidity_failures')
            counts['liquidity_unknown' if failures is None else 'liquidity_rejected' if failures else 'liquidity_pass'] += 1
            liquidity.update(failures or [])
            gate = (p.get('gates') or {}).get('early')
            if p['signal']['metadata'].get('squeeze_entry_timing') == 'early_breakout':
                counts['early_sources'] += 1
                if gate is None:
                    counts['early_gate_unknown'] += 1
            if gate is not None:
                early.update(gate['early_all_gate_failures'])
                if not gate['early_all_gate_failures']:
                    counts['early_rule_pass_ignoring_liquidity'] += 1
            symbols[p['signal']['symbol']] += 1
            sources.append({'source_id': source, 'first_ms': observed, 'status': p['status'],
                'timing': p['signal']['metadata'].get('squeeze_entry_timing'),
                'liquidity_failures': failures, 'impact_status': (p.get('impact') or {}).get('status'),
                'early_failures': gate['early_all_gate_failures'] if gate else None,
                'errors': p['errors']})
        return {'execution': 'OBSERVATION_ONLY', 'meta': meta,
                'scope': 'First hourly source observations; adjacent hours may still be correlated',
                'sources': len(sources), 'observations': db.execute('SELECT count(*) FROM observations').fetchone()[0],
                'coverage': dict(coverage), 'first_source_counts': dict(counts),
                'liquidity_failures': dict(liquidity), 'early_failures': dict(early),
                'symbols': dict(symbols), 'first_sources': sources}
    finally:
        db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    args = parser.parse_args()
    print(json.dumps(report(args.database), indent=2, ensure_ascii=True))
