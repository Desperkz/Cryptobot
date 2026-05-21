from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Callable


class SystemdNotifier:
    def __init__(self, notify_socket: str | None = None) -> None:
        self.notify_socket = notify_socket if notify_socket is not None else os.environ.get("NOTIFY_SOCKET")

    @property
    def enabled(self) -> bool:
        return bool(self.notify_socket)

    def notify(self, message: str) -> bool:
        if not self.notify_socket:
            return False
        address = self.notify_socket
        if address.startswith("@"):
            address = "\0" + address[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(message.encode("utf-8"))
        return True

    def ready(self) -> bool:
        return self.notify("READY=1")

    def watchdog(self, status: str | None = None) -> bool:
        payload = "WATCHDOG=1"
        if status:
            payload += f"\nSTATUS={status}"
        return self.notify(payload)


def start_watchdog_thread(
    status: str,
    interval_sec: float = 20.0,
    stop_when: Callable[[], bool] | None = None,
) -> threading.Thread | None:
    notifier = SystemdNotifier()
    if not notifier.enabled:
        return None
    notifier.ready()

    def _run() -> None:
        while not (stop_when and stop_when()):
            try:
                notifier.watchdog(status)
            except Exception:
                return
            time.sleep(interval_sec)

    thread = threading.Thread(target=_run, name="systemd-watchdog", daemon=True)
    thread.start()
    return thread
