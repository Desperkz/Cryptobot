"""Bounded passive reader of the fixed VWAP exit study; no trading setup."""
import hashlib
import json
import os
from pathlib import Path

STUDY='vwap-exits-20261122'
EXPECTED_SHA256='828fe2af145af5ad7c6bd60bc5b11e3216cdbe6f6f4631b0ee9d963594729c55'


def api_vwap_exit_review(path=None):
    absent={'study':STUDY,'health':'UNAVAILABLE','orders_possible':False,
            'live_variants':[],'combined_with_main_balance':False}
    try:
        if path is None:
            path=Path(os.environ.get('BOT_ROOT','/root/bot_v2_1'))/'docs/research/vwap_exits_20261122/summary.json'
        with Path(path).open('rb') as saved:raw=saved.read(64*1024+1)
        if len(raw)>64*1024 or hashlib.sha256(raw).hexdigest()!=EXPECTED_SHA256:raise ValueError('UNVERIFIED_REVIEW')
        d=json.loads(raw)
        if (d['study']!=STUDY or d['orders_possible'] is not False or d['live_variants']
                or d['automatic_paper_promotion'] is not False or d['combined_with_main_balance'] is not False
                or d['decision']!='BOTH_EXIT_RULES_REJECTED'
                or any(v['historical_launch_checks_passed'] or v['paper_promotion'] for v in d['variants'])):
            raise ValueError('UNEXPECTED_REVIEW')
        return {**d,'artifact_sha256':EXPECTED_SHA256}
    except (OSError,ValueError,TypeError,KeyError,RecursionError) as exc:
        return {**absent,'source_error':type(exc).__name__}
