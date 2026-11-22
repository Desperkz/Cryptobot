"""Read one immutable, dated historical review; never start an experiment."""
import hashlib
import json
import os
from pathlib import Path

STUDY='diversified-v4-review-20261122'
EXPECTED_SHA256='7bcd0554f02cc4895f29d6fd55308ac2ff3c4e5d64dc5d962c76784e602a4542'


def api_diversified_v4_review(path=None):
    absent={'study':STUDY,'health':'UNAVAILABLE','orders_possible':False,
            'live_variants':[],'combined_with_main_balance':False}
    try:
        if path is None:
            path=Path(os.environ.get('BOT_ROOT','/root/bot_v2_1'))/'docs/research/diversified_v4_20261122/summary.json'
        with Path(path).open('rb') as saved:raw=saved.read(64*1024+1)
        if len(raw)>64*1024 or hashlib.sha256(raw).hexdigest()!=EXPECTED_SHA256:
            raise ValueError('UNVERIFIED_REVIEW')
        data=json.loads(raw)
        if (data['study']!=STUDY or data['orders_possible'] is not False
                or data['automatic_paper_promotion'] is not False
                or data['combined_with_main_balance'] is not False or data['live_variants']
                or data['decision']!='NO_VARIANT_PASSED'
                or any(v['historical_launch_checks_passed'] or v['paper_promotion'] for v in data['variants'])):
            raise ValueError('UNEXPECTED_REVIEW')
        return {**data,'artifact_sha256':EXPECTED_SHA256,'is_historical_snapshot':True}
    except (OSError,ValueError,TypeError,KeyError,RecursionError) as exc:
        return {**absent,'source_error':type(exc).__name__}
