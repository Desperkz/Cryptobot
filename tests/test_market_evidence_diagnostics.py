from contextlib import contextmanager
import importlib.util
import json
import logging
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import uuid

import pytest

SCRIPT = Path(__file__).resolve().parents[1]/'scripts/market_evidence_diagnostics_launcher.py'
spec = importlib.util.spec_from_file_location('market_evidence_diagnostics_launcher', SCRIPT)
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)


class NeverFormat:
    def __str__(self):
        raise AssertionError('Untrusted log argument was formatted')


def record(msg, args=(), name='websockets.client', exc_info=None):
    r = logging.LogRecord(name, logging.DEBUG, '', 0, msg, args, exc_info)
    r.websocket = SimpleNamespace(id=uuid.UUID('12345678-1234-5678-1234-567812345678'), latency=.012)
    return r


def read(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.parametrize('opcode', [0, 1, 2])
def test_application_frames_are_rejected_before_formatting(tmp_path, opcode):
    frame = NeverFormat(); frame.opcode = opcode; frame.data = b'API_SECRET_DO_NOT_LOG'
    h = d.SafeTransportHandler(tmp_path/'transport.jsonl')
    h.emit(record('< %s', (frame,)))
    assert h.stats['dropped_application'] == 1
    assert not h.path.exists()
    h.close()


def test_http_headers_and_arbitrary_errors_do_not_format_arguments(tmp_path):
    h = d.SafeTransportHandler(tmp_path/'transport.jsonl')
    for msg in ('> %s: %s', 'GET %s HTTP/1.1', 'Authorization %s', 'arbitrary %s'):
        h.emit(record(msg, (NeverFormat(),)))
    h.emit(record('= connection is %s', (NeverFormat(),)))
    assert h.stats['written'] == 0 and h.stats['dropped_other'] == 5
    h.close()


@pytest.mark.parametrize('opcode,event', [(8, 'close_frame'), (9, 'ping_frame'), (10, 'pong_frame')])
def test_control_frames_log_metadata_without_binary_payload(tmp_path, opcode, event):
    frame = NeverFormat(); frame.opcode = opcode; frame.data = b'\x03\xf3keepalive ping timeout'
    h = d.SafeTransportHandler(tmp_path/'transport.jsonl'); h.emit(record('> %s', (frame,))); h.close()
    value = read(h.path)[0]
    assert value['event'] == event and value['direction'] == 'sent'
    assert value['latency_ms'] == 12
    assert value['connection_id'] == '12345678-1234-5678-1234-567812345678'
    if opcode == 8:
        assert value['close_code'] == 1011 and value['close_reason'] == 'keepalive ping timeout'
    else:
        assert 'close_code' not in value and 'keepalive ping timeout' not in h.path.read_text()


def test_close_reason_is_bounded_redacted_and_control_safe(tmp_path):
    frame = SimpleNamespace(opcode=8, data=b'\x03\xe8token=SECRET\n'+b'x'*300)
    h = d.SafeTransportHandler(tmp_path/'transport.jsonl'); h.emit(record('< %s', (frame,))); h.close()
    value = read(h.path)[0]
    assert value['close_code'] == 1000 and len(value['close_reason']) <= 120
    assert 'SECRET' not in h.path.read_text() and '\n' not in value['close_reason']


@pytest.mark.parametrize('data', [b'', b'\x01', None, NeverFormat()])
def test_malformed_close_is_safe(tmp_path, data):
    h = d.SafeTransportHandler(tmp_path/'transport.jsonl')
    h.emit(record('< %s', (SimpleNamespace(opcode=8, data=data),))); h.close()
    assert read(h.path)[0]['close_code'] is None


@pytest.mark.parametrize('message,event', [
    ('% sent keepalive ping', 'keepalive_ping_sent'),
    ('% received keepalive pong', 'keepalive_pong_received'),
    ('- timed out waiting for keepalive pong', 'keepalive_timeout'),
    ('< EOF', 'eof_received'), ('x closing TCP connection', 'tcp_close')])
def test_known_lifecycle_messages(message, event):
    value, dropped = d.classify(record(message))
    assert dropped is None and value['event'] == event


def test_exception_logging_omits_text_and_traceback(tmp_path):
    e = OSError(104, 'request header token=API_SECRET_DO_NOT_LOG')
    h = d.SafeTransportHandler(tmp_path/'transport.jsonl')
    h.emit(record('keepalive ping failed', exc_info=(type(e), e, None))); h.close()
    value = read(h.path)[0]
    assert value['error_type'] == type(e).__name__ and value['errno'] == 104
    assert 'API_SECRET' not in h.path.read_text()


def test_log_initialization_failure_does_not_raise(tmp_path, capsys):
    blocker = tmp_path/'file'; blocker.write_text('x')
    h = d.SafeTransportHandler(blocker/'transport.jsonl')
    h.emit(record('< EOF')); h.close()
    assert h.stats['errors'] == 1
    assert capsys.readouterr().err.count('collector continues') == 1


def test_log_write_failure_is_fail_open_and_disables_sink(tmp_path, monkeypatch, capsys):
    h = d.SafeTransportHandler(tmp_path/'transport.jsonl')
    monkeypatch.setattr(h.sink, 'emit', lambda _: (_ for _ in ()).throw(OSError('token=PRIVATE')))
    h.emit(record('< EOF')); h.emit(record('< EOF')); h.close()
    assert h.sink.failed and h.stats['errors'] == 1
    assert 'PRIVATE' not in capsys.readouterr().err


def test_rotation_stays_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(d, 'LOG_BYTES', 512)
    h = d.SafeTransportHandler(tmp_path/'transport.jsonl')
    for _ in range(100): h.emit(record('< EOF'))
    h.close()
    files = list(tmp_path.glob('transport.jsonl*'))
    assert len(files) == 3
    assert all(path.stat().st_size <= 512 for path in files)
    assert all(json.loads(line)['event'] == 'eof_received' for path in files for line in path.read_text().splitlines())


def test_launcher_forwards_collector_args_and_restores_scoped_logger(tmp_path, monkeypatch):
    logger = logging.getLogger('websockets.client')
    original = logger.level, logger.propagate, logger.disabled, logger.handlers[:], logger.filters[:]
    root = logging.getLogger(); root_before = root.level, root.handlers[:]
    argv = sys.argv; seen = []
    def run(module, run_name):
        seen.append((module, run_name, sys.argv[:]))
        assert logger.level == logging.DEBUG and logger.propagate is False
        logger.debug('< %s', SimpleNamespace(opcode=1, data=b'SECRET'))
        logger.debug('= connection is %s', 'OPEN')
    monkeypatch.setattr(d.runpy, 'run_module', run)
    path = tmp_path/'transport.jsonl'
    d.main(['--data-dir', '/tmp/public-data', '--transport-log', str(path), '--duration', '12'])
    assert seen == [(d.MODULE, '__main__', [d.MODULE, '--data-dir', '/tmp/public-data', '--duration', '12'])]
    assert sys.argv is argv
    assert (logger.level, logger.propagate, logger.disabled, logger.handlers, logger.filters) == original
    assert (root.level, root.handlers) == root_before
    rows = read(path)
    assert rows[0]['event'] == 'diagnostic_start' and len(rows[0]['launcher_sha256']) == 64
    assert rows[-1]['event'] == 'diagnostic_stop' and rows[-1]['statistics']['dropped_application'] == 1
    assert 'SECRET' not in path.read_text()


def test_collector_exception_is_not_swallowed_and_context_restored(tmp_path, monkeypatch):
    argv = sys.argv
    monkeypatch.setattr(d.runpy, 'run_module', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('source failed')))
    with pytest.raises(RuntimeError, match='source failed'):
        d.main(['--transport-log', str(tmp_path/'transport.jsonl'), '--data-dir', '/tmp/source'])
    assert sys.argv is argv


def test_optional_metadata_failure_does_not_prevent_collector(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(d.importlib.metadata, 'version', lambda _: (_ for _ in ()).throw(OSError('metadata denied')))
    monkeypatch.setattr(d.runpy, 'run_module', lambda *a, **k: called.append(True))
    d.main(['--transport-log', str(tmp_path/'transport.jsonl'), '--data-dir', '/tmp/source'])
    assert called == [True]


def test_nonfinite_latency_is_omitted_without_exposing_frame(tmp_path):
    r = record('< EOF'); r.websocket.latency = 1e308
    h = d.SafeTransportHandler(tmp_path/'transport.jsonl'); h.emit(r); h.close()
    assert 'latency_ms' not in read(h.path)[0]


def test_dropped_frame_benchmark_reports_overhead(tmp_path):
    frame = SimpleNamespace(opcode=1, data=b'never logged')
    logger = logging.getLogger('websockets.client'); samples = 100_000
    prior = logger.level
    logger.setLevel(logging.INFO)
    start = time.perf_counter()
    for _ in range(samples): logger.debug('< %s', frame)
    baseline = time.perf_counter()-start
    logger.setLevel(prior)
    with d.scoped_logging(tmp_path/'benchmark.jsonl') as handler:
        start = time.perf_counter()
        for _ in range(samples): logger.debug('< %s', frame)
        diagnostics = time.perf_counter()-start
        assert handler.stats['dropped_application'] == samples
    print(json.dumps({'samples': samples, 'baseline_seconds': baseline,
                      'diagnostics_seconds': diagnostics,
                      'extra_us_per_frame': (diagnostics-baseline)/samples*1e6}))
