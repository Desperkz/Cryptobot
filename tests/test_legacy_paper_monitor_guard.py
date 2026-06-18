import pytest

import paper_monitor


def test_legacy_paper_monitor_is_disabled() -> None:
    with pytest.raises(SystemExit) as exc_info:
        paper_monitor.main()

    message = str(exc_info.value)
    assert "paper_monitor.py is deprecated" in message
    assert "paper_monitor_v2.py" in message
