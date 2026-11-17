"""Logging-only launcher for the unchanged public market collector.

It doesn't patch websocket, event-loop, Store, data or rule behavior. Application
frames and HTTP records are rejected before message formatting. Diagnostic I/O
failure disables this optional log while the original collector keeps running.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import importlib.metadata
import json
import logging
from logging.handlers import RotatingFileHandler
import math
from pathlib import Path
import platform
import re
import runpy
import sys
import time
import uuid

MODULE = 'trading_bot.research.market_evidence_collector'
LOG_BYTES = 1024 * 1024
LOG_BACKUPS = 2
_SECRET = re.compile(r'(?i)\b(authorization|api[-_]?key|token|signature)\s*[:=]\s*\S+')


def _text(value, limit=120):
    if not isinstance(value, str):
        return None
    value = ''.join(c if c.isprintable() else '?' for c in value[:256])
    return _SECRET.sub(lambda m: m.group(1)+'=[redacted]', value)[:limit]


def _base(record):
    stamp = record.created
    if not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
        stamp = time.time()
    result = {'at_ms': int(stamp * 1000), 'level': record.levelname}
    connection = getattr(record, 'websocket', None)
    identity = getattr(connection, 'id', None)
    if isinstance(identity, uuid.UUID):
        result['connection_id'] = str(identity)
    elif isinstance(identity, str):
        try:
            result['connection_id'] = str(uuid.UUID(identity))
        except ValueError:
            pass
    latency = getattr(connection, 'latency', None)
    if (not isinstance(latency, bool) and isinstance(latency, (float, int))
            and math.isfinite(latency) and latency >= 0):
        milliseconds = latency * 1000
        if math.isfinite(milliseconds):
            result['latency_ms'] = milliseconds
    return result


def classify(record):
    """Return (safe event or None, rejection kind), without getMessage()."""
    if record.name != 'websockets.client' or not isinstance(record.msg, str):
        return None, 'other'
    msg = record.msg
    if msg in ('< %s', '> %s'):
        # Library Frame.__str__ would expose whole TEXT/BINARY payloads. Don't
        # format or stringify any arguments, even for rejected malformed frames.
        if not isinstance(record.args, tuple) or len(record.args) != 1:
            return None, 'other'
        frame = record.args[0]
        opcode = getattr(frame, 'opcode', None)
        if isinstance(opcode, bool) or not isinstance(opcode, int):
            return None, 'other'
        if opcode in (0, 1, 2):
            return None, 'application'
        if opcode not in (8, 9, 10):
            return None, 'other'
        event = _base(record)
        event.update(event={8: 'close_frame', 9: 'ping_frame', 10: 'pong_frame'}[opcode],
                     direction='received' if msg[0] == '<' else 'sent')
        if opcode == 8:
            data = getattr(frame, 'data', None)
            if not isinstance(data, (bytes, bytearray, memoryview)):
                event.update(close_code=None, close_reason=None, malformed_close=True)
            else:
                data = bytes(data[:125])
                event['close_code'] = int.from_bytes(data[:2], 'big') if len(data) >= 2 else None
                event['close_reason'] = _text(data[2:].decode('utf-8', 'replace')) if len(data) >= 2 else None
                if len(data) == 1:
                    event['malformed_close'] = True
        return event, None
    if msg == '= connection is %s':
        state = record.args[0] if isinstance(record.args, tuple) and len(record.args) == 1 else None
        if state not in ('CONNECTING', 'OPEN', 'CLOSING', 'CLOSED'):
            return None, 'other'
        return {**_base(record), 'event': 'connection_state', 'state': state}, None
    events = {
        '% sent keepalive ping': 'keepalive_ping_sent',
        '% received keepalive pong': 'keepalive_pong_received',
        '- timed out waiting for keepalive pong': 'keepalive_timeout',
        '< EOF': 'eof_received', '> EOF': 'eof_sent',
        'x half-closing TCP connection': 'tcp_half_close',
        'x closing TCP connection': 'tcp_close',
        'keepalive ping failed': 'keepalive_error',
        '! error while sending data': 'transport_send_error',
        'unexpected internal error': 'internal_transport_error',
        'parser failed': 'protocol_parser_error',
        'connection closed': 'connection_closed',
    }
    if msg not in events:
        return None, 'other'
    event = {**_base(record), 'event': events[msg]}
    # Do not render exception text or traceback: request headers / server text
    # can be included there. The error class plus close frames gives context.
    info = record.exc_info
    if isinstance(info, tuple) and len(info) >= 2 and isinstance(info[0], type):
        event['error_type'] = _text(info[0].__name__, 80)
        errno = getattr(info[1], 'errno', None)
        if isinstance(errno, int) and not isinstance(errno, bool):
            event['errno'] = errno
    return event, None


class _Rotator(RotatingFileHandler):
    def handleError(self, record):
        # logging's default error handler can print a traceback and arguments.
        # Keep optional diagnostics fail-open and entirely payload-free.
        self.failed = True


class SafeTransportHandler(logging.Handler):
    def __init__(self, path):
        super().__init__(logging.DEBUG)
        self.path = Path(path)
        self.sink = None
        self.warned = False
        self.stats = {'written': 0, 'dropped_application': 0, 'dropped_other': 0, 'errors': 0}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.sink = _Rotator(self.path, maxBytes=LOG_BYTES, backupCount=LOG_BACKUPS,
                                 encoding='utf-8', delay=True)
            self.sink.failed = False
            self.sink.setFormatter(logging.Formatter('%(message)s'))
        except Exception:
            self._failed()

    def _failed(self):
        self.stats['errors'] += 1
        if not self.warned:
            self.warned = True
            try:
                sys.stderr.write('Optional transport diagnostics unavailable; collector continues.\n')
            except Exception:
                pass

    def write_event(self, value):
        if self.sink is None or self.sink.failed:
            return
        try:
            raw = json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
            # Every event is fixed-field metadata. A final cap also protects
            # against future classifier additions accidentally expanding logs.
            if len(raw) > 2048:
                self._failed()
                return
            safe = logging.LogRecord('collector.transport', logging.INFO, '', 0, raw, (), None)
            self.sink.emit(safe)
            if self.sink.failed:
                self._failed()
            else:
                self.stats['written'] += 1
        except Exception:
            if self.sink is not None:
                self.sink.failed = True
            self._failed()

    def emit(self, record):
        try:
            event, rejected = classify(record)
            if event is not None:
                self.write_event(event)
            elif rejected == 'application':
                self.stats['dropped_application'] += 1
            else:
                self.stats['dropped_other'] += 1
        except Exception:
            self._failed()

    def close(self):
        if self.sink is not None:
            try:
                self.sink.close()
            except Exception:
                self._failed()
        super().close()


@contextmanager
def scoped_logging(path):
    logger = logging.getLogger('websockets.client')
    prior = logger.level, logger.propagate, logger.disabled, logger.handlers[:], logger.filters[:]
    handler = SafeTransportHandler(path)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.disabled = False
    logger.handlers = [handler]
    logger.filters = []
    try:
        try:
            try:
                websocket_version = importlib.metadata.version('websockets')
            except importlib.metadata.PackageNotFoundError:
                websocket_version = 'unavailable'
            handler.write_event({'event': 'diagnostic_start', 'at_ms': int(time.time()*1000),
                                 'pid': __import__('os').getpid(), 'python': platform.python_version(),
                                 'websockets': websocket_version, 'collector_module': MODULE,
                                 'launcher_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                                 'max_file_bytes': LOG_BYTES, 'backups': LOG_BACKUPS})
        except Exception:
            handler._failed()
        yield handler
    finally:
        handler.write_event({'event': 'diagnostic_stop', 'at_ms': int(time.time()*1000),
                             'statistics': dict(handler.stats)})
        logger.setLevel(prior[0]); logger.propagate = prior[1]; logger.disabled = prior[2]
        logger.handlers = prior[3]; logger.filters = prior[4]
        handler.close()


def main(argv=None):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--transport-log', type=Path, required=True)
    options, collector_args = parser.parse_known_args(argv)
    original = sys.argv
    try:
        sys.argv = [MODULE, *collector_args]
        with scoped_logging(options.transport_log):
            runpy.run_module(MODULE, run_name='__main__')
    finally:
        sys.argv = original


if __name__ == '__main__':
    main()
