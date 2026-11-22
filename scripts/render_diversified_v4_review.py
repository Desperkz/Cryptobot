"""Publish a fixed historical review, including failures, without launching trades."""
import argparse
import hashlib
import html
import json
from pathlib import Path

LABELS={
    'VWAP_REVERSION_V4_TREND_GUARD':('Возврат к VWAP','Не входить против часового тренда','Почти весь запас прибыли съедают расходы. Положительный итог зависит от лучшей сделки и первой половины периода.'),
    'SWEEP_RECLAIM_V4_STRUCTURE':('Ложный пробой','Закрытие за противоположным краем свечи выноса','Более строгое подтверждение не отделило удачные входы: средний R ухудшился. Убыток есть ещё до расходов.'),
    'MOMENTUM_CONTINUATION_V4_EXTENSION':('Продолжение импульса','Не дальше одного часового ATR от EMA20','Ограничение удаления убрало 21 прибыльную сделку и оставило 20 убыточных из 22. На этой выборке гипотеза неверна.'),
}
CHECKS={
    'min_accepted_closed':'Не менее 12 закрытых входов',
    'min_retained_fraction':'Сохранено не менее 15% входов',
    'min_accepted_utc_days':'Входы как минимум в 5 разных дней UTC',
    'positive_primary_net':'Чистый результат выше нуля',
    'positive_paired_delta':'Итог лучше сопоставимого контроля',
    'nonnegative_10bps_net':'При проскальзывании 10 б.п. итог не отрицательный',
    'nonnegative_both_calendar_halves':'Обе календарные половины не убыточны',
    'nonnegative_without_best_trade':'Без лучшей сделки итог не отрицательный',
}


def formatted(value):return f'{value:+.2f}'.replace('.',',')


def publish(review, directory):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    summary={k:v for k,v in review.items() if k!='rows'}
    summary.update(study='diversified-v4-review-20261122',health='OK',
                   live_variants=[],automatic_paper_promotion=False,
                   decision='NO_VARIANT_PASSED',combined_with_main_balance=False)
    # This publisher is for this rejected cohort; a future positive experiment
    # needs its own registration and a separate presentation contract.
    if any(v['historical_launch_checks_passed'] for v in summary['variants']):
        raise ValueError('NEW_PROSPECTIVE_PROTOCOL_REQUIRED')
    cards=[]
    for v in summary['variants']:
        label,change,conclusion=LABELS[v['variant']]
        v.update(label=label,change_ru=change,conclusion_ru=conclusion,
                 failed_check_labels=[CHECKS[k] for k in v['failed_checks']])
        a,b=v['accepted'],v['control']
        cells=''.join(f'<div><small>{html.escape(k)}</small><strong class="{"positive" if n>=0 else "negative"}">{formatted(n)}</strong></div>' for k,n in (
            ('Контроль V3 · USDT',b['net_usdt']),('После фильтра · USDT',a['net_usdt']),('Стресс 10 б.п. · USDT',v['stress10']['net_usdt'])))
        cards.append(f'''<article><span class="tag">Не прошла проверку</span><h2>{html.escape(label)}</h2>
        <p>{html.escape(change)}</p><div class="metrics">{cells}</div>
        <p>Сохранено {a['closed']} из {b['closed']} входов ({v['retained_fraction']:.0%}). Средний R: {b['average_r']:+.3f} → {a['average_r']:+.3f}.</p>
        <p>Предотвращённые убытки: {formatted(v['prevented_losses_usdt'])} USDT. Упущенная прибыль: {formatted(v['missed_profit_usdt'])} USDT ({v['missed_profitable_trades']} сделок).</p>
        <p>Движение цены до расходов: {formatted(v['accepted_price_before_expenses_usdt'])} USDT; расходы: {v['accepted_expenses_usdt']:.2f} USDT.</p>
        <p><strong>{html.escape(conclusion)}</strong></p><details><summary>Проверка устойчивости</summary>
        <p>Без лучшей сделки: {formatted(v['without_best_net_usdt'])} USDT.</p><p>Половины периода: {formatted(v['halves'][0]['net_usdt'])} / {formatted(v['halves'][1]['net_usdt'])} USDT.</p>
        <ul>{''.join('<li>'+('✓ ' if passed else '✕ ')+html.escape(CHECKS[k])+'</li>' for k,passed in v['checks'].items())}</ul></details></article>''')
    payload=(json.dumps(summary,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+'\n').encode('utf-8')
    (directory/'summary.json').write_bytes(payload)
    page='''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Проверка V4 · 22 ноября 2026</title>
    <style>body{background:#0d1420;color:#edf2fa;font:16px/1.6 system-ui;margin:0}main{max-width:1100px;margin:auto;padding:32px 20px}h1{font-size:32px;line-height:1.2}h2{font-size:23px;margin:12px 0}p{color:#c1cede}article,.intro{border:1px solid #304157;border-radius:14px;padding:24px;margin:20px 0;background:#151f2f}.intro{border-top:3px solid #91baff}.tag{color:#f1cb78;font-size:14px}.metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;margin:22px 0}.metrics div{background:#0d1420;border-radius:10px;padding:16px}small{display:block;color:#a2b2c7}strong{font-weight:650}.metrics strong{display:block;font-size:30px;font-variant-numeric:tabular-nums}.positive{color:#60deb0}.negative{color:#ff8b98}summary{cursor:pointer}code{overflow-wrap:anywhere}@media(max-width:600px){.metrics{grid-template-columns:1fr}main{padding:18px 14px}h1{font-size:26px}article,.intro{padding:18px}}</style><main>
    <span class="tag">ИССЛЕДОВАНИЯ · 22.11.2026 · ФИКСИРОВАННЫЙ СРЕЗ</span><h1>Меньше убытков ещё не означает прибыльную стратегию</h1>
    <div class="intro"><h2>Ни один из трёх вариантов не допущен к запуску</h2><p>Проверены 255 закрытых сделок с восстановлением исходных планов, расходов и признаков. Правила зафиксированы до их нового сопоставления с результатами. Исторический период ранее изучался: это исследование, а не независимое подтверждение.</p>
    <p>Фильтры уменьшают число входов. Освободившиеся места не заполнялись другими сигналами: это сравнение тех же исходных сделок, а не результат самостоятельного портфеля.</p></div>'''+''.join(cards)+'''
    <article><h2>Что сохраняется в работающем боте</h2><p>V3 и прежние исследования продолжают собирать контрольные данные. Ни одна новая торговая версия не запущена, риск и расходы не снижены. История не обнулена.</p><h2>Условия расчёта</h2><p>2 USDT на полный стоп с расходами, комиссия 4 б.п. с каждой стороны, исходное проскальзывание 5 б.п. с каждой стороны, неблагоприятный funding 1 б.п. / 8 ч. Стресс: 10 б.п. с каждой стороны, исходные уровни и время выхода, объём пересчитан под те же 2 USDT риска. Исполнение на бирже не воспроизводилось.</p>
    <p>Следующая гипотеза должна менять причину убытка: для VWAP проверять способы сохранить движение до расходов; для ложного пробоя и импульса пересматривать сам вход. Пороги в этой проверке после результатов не менялись.</p></article></main></html>'''
    (directory/'report.html').write_bytes(page.encode('utf-8'))
    return hashlib.sha256(payload).hexdigest()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--review',required=True);p.add_argument('--directory',required=True);a=p.parse_args()
    print(publish(json.loads(Path(a.review).read_text(encoding='utf-8')),a.directory))
