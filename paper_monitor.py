"""Legacy paper monitor entrypoint kept only as a deprecation guard.

The active monitor for bot v2.1 is `paper_monitor_v2.py` and the corresponding
systemd unit `paper-monitor-v2-1.service`. Leaving the old script runnable is
dangerous because it points at a legacy DB path and bypasses the realistic
execution model used by the current bot.
"""

from __future__ import annotations


def main() -> None:
    raise SystemExit(
        "paper_monitor.py is deprecated and intentionally disabled. "
        "Use paper_monitor_v2.py instead."
    )


if __name__ == "__main__":
    main()
