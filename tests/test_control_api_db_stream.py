from __future__ import annotations

from io import BytesIO
from unittest.mock import Mock

import bot_control_v2 as control


def test_db_download_streams_file_without_loading_it_as_one_response(monkeypatch, tmp_path) -> None:
    database_path = tmp_path / "trading.sqlite3"
    payload = b"x" * (2 * 1024 * 1024 + 17)
    database_path.write_bytes(payload)
    monkeypatch.setattr(control, "DB_PATH", str(database_path))

    handler = object.__new__(control.Handler)
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    handler.wfile = BytesIO()

    handler._send_db()

    assert handler.wfile.getvalue() == payload
    handler.send_response.assert_called_once_with(200)
    assert ("Content-Length", str(len(payload))) in [call.args for call in handler.send_header.call_args_list]
