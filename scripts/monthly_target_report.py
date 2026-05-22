from __future__ import annotations

import argparse
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch and save the bot v2.1 monthly target report.")
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8888/monthly-target-plan",
        help="Control API monthly target endpoint.",
    )
    parser.add_argument(
        "--output-dir",
        default="data/reports",
        help="Directory for timestamped JSON reports.",
    )
    args = parser.parse_args()

    with urllib.request.urlopen(args.url, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if "error" in payload:
        raise SystemExit(f"monthly target report failed: {payload['error']}")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    report_path = output_dir / f"monthly_target_report_{stamp}.json"
    latest_path = output_dir / "monthly_target_report_latest.json"
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    report_path.write_text(text, encoding="utf-8")
    latest_path.write_text(text, encoding="utf-8")
    print(str(report_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
