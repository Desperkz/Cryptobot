"""Read-only report for the frozen mainnet paper experiment (stdlib only)."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import time
import zlib

ARMS = ('BASELINE_2R', 'CURRENT_GATE_2R', 'CURRENT_GATE_PROFILE', 'P8_OBSERVE_PROFILE')
POLICIES = ('FIRST_OBSERVATION', 'FIRST_ADMISSIBLE')


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def freshness(record, field, now_ms, limit_sec):
    stamp = record.get(field)
    age = (now_ms - stamp) / 1000 if stamp is not None else None
    state = ('UNKNOWN' if age is None else 'CLOCK_SKEW' if age < -5 else
             'STALE' if age > limit_sec else 'FRESH')
    return {'state': state, 'age_sec': age, 'limit_sec': limit_sec,
            'last_status': record.get('status')}


def build_report(path, now_ms=None):
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=15)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')  # One consistent snapshot, including WAL records.
        meta = {r['key']: json.loads(r['value']) for r in db.execute('SELECT * FROM meta')}
        manifest = meta.get('manifest', {})
        groups = {}
        for policy in manifest.get('policies', POLICIES):
            for arm in manifest.get('arms', ARMS):
                groups[policy, arm] = {'policy': policy, 'arm': arm, 'statuses': Counter(),
                                      'r': [], 'pnl': [], 'closed_missing_result': 0}
        episodes = {}
        for row in db.execute('SELECT * FROM positions ORDER BY entry_ms,id'):
            group = groups[row['policy'], row['arm']]
            group['statuses'][row['status']] += 1
            # Invalid entries and unexecuted pending records are not filled episodes.
            if row['status'] in ('OPEN', 'CLOSED'):
                episodes.setdefault(row['source_id'], (row['symbol'], row['direction'], row['entry_ms']))
            if row['status'] != 'CLOSED':
                continue
            result = (json.loads(row['result']) if row['result'] else None) or {}
            r, pnl = number(result.get('R')), number(result.get('model_pnl_usdt'))
            if result.get('status') != 'CLOSED' or r is None or pnl is None:
                group['closed_missing_result'] += 1
                continue
            group['r'].append(r)
            group['pnl'].append(pnl)
        arms = []
        for group in groups.values():
            values, pnl = group.pop('r'), group.pop('pnl')
            positive = sum(v for v in values if v > 0)
            negative = -sum(v for v in values if v < 0)
            group.update(closed_measured=len(values),
                         mean_R=sum(values)/len(values) if values else None,
                         realized_model_pnl_usdt=sum(pnl) if pnl else None,
                         profit_factor=positive/negative if negative else None,
                         profit_factor_state='FINITE' if negative else 'NO_LOSSES' if values else 'NO_DATA',
                         win_rate=sum(v > 0 for v in values)/len(values) if values else None)
            group['statuses'] = dict(group['statuses'])
            arms.append(group)
        # A conservative first-entry-per-60-minute proxy, not proof of independence.
        anchors = {}
        clusters = 0
        for symbol, direction, stamp in sorted(episodes.values(), key=lambda e: e[2]):
            key = symbol, direction
            if key not in anchors or stamp - anchors[key] >= 3600000:
                anchors[key] = stamp
                clusters += 1
        coverage = []
        for row in db.execute('''SELECT c.* FROM coverage c JOIN
                (SELECT symbol,max(id) id FROM coverage GROUP BY symbol) last ON c.id=last.id'''):
            payload = json.loads(row['payload'])
            oi = payload.get('metrics', {}).get('open_interest_change_pct')
            stamps = [item['timestamp'] for call in payload.get('oi_requests', {}).values()
                      for item in call.get('data', []) if isinstance(item, dict) and 'timestamp' in item]
            coverage.append({'symbol': row['symbol'], 'at_ms': row['at_ms'],
                'status': payload.get('status'), 'oi_present': number(oi) is not None,
                'oi_state': payload.get('oi_state'), 'oi_change_pct': number(oi),
                'oi_history_age_sec': (now_ms-max(stamps))/1000 if stamps else None})
        failures, outcomes = Counter(), Counter()
        corrupt = 0
        for row in db.execute('''SELECT o.payload_zlib FROM observations o JOIN
                (SELECT source_id,min(id) id FROM observations GROUP BY source_id) first ON o.id=first.id'''):
            try:
                payload = json.loads(zlib.decompress(row[0]))
                decision = payload['decisions']
                for reason in set(decision.get('current_all_gate_failures', [])):
                    failures['current:' + reason] += 1
                for flag in set(decision.get('current_structural_flags', [])):
                    failures['current_structure:' + flag] += 1
                p8 = decision.get('p8_all_gates', {})
                for flag in set(p8.get('structural_flags', [])):
                    failures['p8_structure:' + flag] += 1
                if p8.get('context'):
                    failures['p8_context:' + p8['context']] += 1
                for name in ('relative_strength', 'retest_or_strong', 'structure_confirmation'):
                    if p8.get(name) is False:
                        failures['p8:' + name] += 1
                for item in payload.get('admission_outcomes', []):
                    outcomes[(item['policy'], item['arm'], item['result'])] += 1
            except (ValueError, KeyError, TypeError, zlib.error):
                corrupt += 1
        return {'generated_ms': now_ms, 'manifest': manifest, 'started_ms': meta.get('started_ms'),
            'scan': meta.get('scan', {}), 'monitor': meta.get('monitor', {}),
            'freshness': {'scan': freshness(meta.get('scan', {}), 'finished_ms', now_ms, 720),
                          'monitor': freshness(meta.get('monitor', {}), 'at_ms', now_ms, 120)},
            'observations': db.execute('SELECT count(*) FROM observations').fetchone()[0],
            'sources': db.execute('SELECT count(*) FROM sources').fetchone()[0],
            'filled_unique_sources': len(episodes), 'first_per_60m_source_proxy': clusters,
            'coverage': coverage, 'arms': arms, 'first_observation_gate_failures': dict(failures),
            'first_observation_admission_outcomes': [dict(policy=p, arm=a, outcome=o, count=n)
                for (p, a, o), n in sorted(outcomes.items())], 'unreadable_first_observations': corrupt,
            'limits': ['Virtual arms and timing policies are correlated; never add their results.',
                       'OPEN mark-to-market results are excluded from realized metrics.',
                       '60-minute grouping is a proxy, not statistical independence.',
                       'Gate reasons overlap; admission counts here use first observations only.',
                       'Fixed universe and admission-only simulation; not a full production portfolio.',
                       'No conclusion about profitability or live readiness follows from this report.']}
    finally:
        db.close()


def markdown(report):
    def fmt(value):
        return '—' if value is None else f'{value:.3f}'
    stamp = datetime.fromtimestamp(report['generated_ms']/1000, timezone.utc).isoformat()
    lines = ['# Mainnet paper: сравнение вариантов', '', f'Снимок: {stamp}', '',
        f"Когорта: `{report['manifest'].get('cohort')}`. Источник: `{report['manifest'].get('data_venue')}`.", '',
        'Режим: виртуальная торговля. Это исследование допуска сигналов, а не полный портфель бота.', '',
        f"Наблюдений: {report['observations']}; исходных сигналов: {report['sources']}; "
        f"источников исполненных позиций: {report['filled_unique_sources']}; "
        f"после группировки по 60 минутам: {report['first_per_60m_source_proxy']}.", '',
        f"OI доступен: {sum(c['oi_present'] for c in report['coverage'])}/{len(report['coverage'])} инструментов в последних записях.", '',
        '| Проверка | Свежесть | Возраст, сек | Последний статус |', '|---|---|---:|---|']
    for name, item in report['freshness'].items():
        lines.append(f"| {name} | {item['state']} | {fmt(item['age_sec'])} | {item['last_status']} |")
    lines += ['', '| Политика | Вариант | Ожидают | Открыты | Закрыты | Неверный вход | Нет результата | Среднее R | PnL, виртуальные USDT | PF |',
              '|---|---|---:|---:|---:|---:|---:|---:|---:|---|']
    for arm in report['arms']:
        s = arm['statuses']
        pf = fmt(arm['profit_factor']) if arm['profit_factor'] is not None else arm['profit_factor_state']
        lines.append(f"| {arm['policy']} | {arm['arm']} | {s.get('PENDING',0)} | {s.get('OPEN',0)} | "
            f"{s.get('CLOSED',0)} | {s.get('INVALID_ENTRY',0)} | {arm['closed_missing_result']} | {fmt(arm['mean_R'])} | "
            f"{fmt(arm['realized_model_pnl_usdt'])} | {pf} |")
    lines += ['', 'Связанные виртуальные копии нельзя складывать как независимые сделки. '
              'Прочерк означает отсутствие результата. Незакрытые позиции исключены из доходности. '
              'Группировка по 60 минутам не доказывает независимость.', '',
              '## Отказы на первом наблюдении', '', 'Причины пересекаются; их сумма не равна числу сигналов.', '']
    lines += [f'- `{key}`: {n}' for key, n in sorted(report['first_observation_gate_failures'].items())] or ['Отказов пока не зарегистрировано.']
    lines += ['', '## Допуски на первом наблюдении', '']
    lines += [f"- `{item['policy']}` / `{item['arm']}` / `{item['outcome']}`: {item['count']}"
              for item in report['first_observation_admission_outcomes']] or ['Наблюдений пока нет.']
    if report['unreadable_first_observations']:
        lines += ['', f"Ошибка чтения наблюдений: {report['unreadable_first_observations']}. Статистика отказов неполная."]
    lines += ['', 'Отчёт не устанавливает прибыльность и готовность к торговле реальными деньгами.', '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--json-out')
    parser.add_argument('--markdown-out')
    args = parser.parse_args()
    source = Path(args.db).resolve()
    # Prevent output options from overwriting the database or its SQLite sidecars.
    outputs = [Path(p).resolve() for p in (args.json_out, args.markdown_out) if p]
    protected = {source, *(Path(str(source)+suffix) for suffix in ('-wal', '-shm', '-journal'))}
    if any(p in protected for p in outputs) or len(set(outputs)) != len(outputs):
        parser.error('Outputs must be distinct and must not overwrite SQLite files')
    report = build_report(source)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    if args.markdown_out:
        Path(args.markdown_out).write_text(markdown(report), encoding='utf-8')
    if not outputs:
        print(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    main()
