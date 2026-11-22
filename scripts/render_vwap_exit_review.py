"""Present rejected VWAP exit hypotheses without claiming a live cohort."""
import argparse
import hashlib
import html
import json
from pathlib import Path

LABELS={'VWAP_TARGET_1R':'Более ранняя цель +1R','VWAP_PROTECT_1R':'Защита стопом после +1R'}


def publish(directory):
    directory=Path(directory);full=json.loads((directory/'review.json').read_text(encoding='utf-8'))
    if any(v['historical_launch_checks_passed'] for v in full['variants']):raise ValueError('NEW_FUTURE_PROTOCOL_REQUIRED')
    summary={k:v for k,v in full.items() if k not in ('rows','diagnostic_rows')}
    summary.update(health='OK',is_historical_snapshot=True,live_variants=[],decision='BOTH_EXIT_RULES_REJECTED',
                   automatic_paper_promotion=False,combined_with_main_balance=False)
    for v in summary['variants']:v['label']=LABELS[v['variant']]
    raw=(json.dumps(summary,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+'\n').encode('utf-8')
    (directory/'summary.json').write_bytes(raw)
    def f(n):return f'{n:+.2f}'.replace('.',',')
    cards=[]
    for v in summary['variants']:
        cards.append(f'''<article><span class="tag">Отклонено · не запущено</span><h2>{html.escape(v['label'])}</h2>
        <div class="metrics"><div><small>Чистый результат · USDT</small><strong>{f(v['result']['net_usdt'])}</strong></div><div><small>Разница с контролем · USDT</small><strong>{f(v['paired_delta_usdt'])}</strong></div><div><small>Стресс 10 б.п. · USDT</small><strong>{f(v['stress10']['net_usdt'])}</strong></div></div>
        <p>Улучшено {v['improved_pairs']}, ухудшено {v['worsened_pairs']}, без изменений {v['unchanged_pairs']} из 26 сделок.</p>
        <p>Улучшения дали {f(v['gained_usdt'])} USDT, ухудшения забрали {v['given_up_usdt']:.2f} USDT.</p>
        <p>Половины периода: {f(v['halves'][0]['net_usdt'])} / {f(v['halves'][1]['net_usdt'])} USDT; без лучшей сделки: {f(v['without_best_net_usdt'])} USDT.</p></article>''')
    page='''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Выходы VWAP · 22.11.2026</title>
    <style>body{background:#0d1420;color:#edf2fa;font:16px/1.6 system-ui;margin:0}main{max-width:1050px;margin:auto;padding:28px 20px}h1{font-size:31px;line-height:1.2}h2{font-size:23px}p{color:#c1cede}article,.intro{border:1px solid #304157;border-radius:14px;padding:24px;margin:20px 0;background:#151f2f}.intro{border-top:3px solid #91baff}.tag{color:#f1cb78;font-size:14px}.metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;margin:22px 0}.metrics div{background:#0d1420;border-radius:10px;padding:16px}small{display:block;color:#a2b2c7}.metrics strong{display:block;color:#ff8b98;font-size:30px;font-variant-numeric:tabular-nums}table{border-collapse:collapse;width:100%}td,th{padding:12px;border-bottom:1px solid #304157;text-align:left}code{overflow-wrap:anywhere}@media(max-width:600px){.metrics{grid-template-columns:1fr}main{padding:18px 14px}h1{font-size:25px}article,.intro{padding:18px}}</style><main>
    <span class="tag">VWAP · ВЫХОДЫ · ФИКСИРОВАННАЯ ПРОВЕРКА 22.11.2026</span><h1>Ранние выходы ухудшили результат</h1>
    <div class="intro"><h2>Сначала нужен более надёжный вход</h2><p>Контроль: те же 26 VWAP-входов после ранее зафиксированного фильтра часового тренда, net +1,32 USDT. Это отдельная подвыборка, не все 54 VWAP-сделки V3.</p>
    <p>Из 15 убыточных сделок только 1 закрыла минуту на +1R до исходного выхода; только 2 достигли +0,5R по закрытию минуты. Защитному выходу обычно нечего было сохранять. Последняя минута исходного выхода исключена из этой диагностики из-за неизвестного порядка внутриминутных касаний.</p>
    <p>Оба варианта не прошли зафиксированные проверки. Новые торговые версии не запущены, старые стратегии и история сохранены.</p></div>'''+''.join(cards)+'''
    <article><h2>Что именно проверено</h2><p>Цель +1R: ближайшая из исходной цели VWAP и уровня +1 исходный ценовой R от эффективного входа. Защита: после закрытия полной минуты на +1R стоп переносится на эффективную цену входа со следующей минуты. Один R — расстояние от эффективного входа до исходного стопа. Стоп на входе всё ещё оплачивает расходы.</p>
    <p>Все 26 исходных выходов воспроизведены по закрытым минутным свечам с совпадением времени, причины, net и всех расходов. Полные 8-часовые окна содержат 12480 строк, 12284 уникальные минуты. Только публичные данные Binance.</p>
    <p>Исходные входы, объём, риск 2 USDT, стоп и срок 8 ч сохранены. Комиссия 4 б.п. с каждой стороны, проскальзывание 5 б.п. с каждой стороны, неблагоприятный funding 1 б.п. / 8 ч по фактическому модельному времени удержания. Стресс 10 б.п. пересчитывает объём под тот же полный риск и сохраняет уровни и время выходов.</p>
    <p>Это исследование ранее изученной истории. Новые входы после досрочного выхода не добавлялись; доходностью самостоятельного портфеля результат не является. Пороги не подбирались по полученным исходам.</p></article></main></html>'''
    (directory/'report.html').write_bytes(page.encode('utf-8'))
    return hashlib.sha256(raw).hexdigest()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--directory',required=True);a=p.parse_args();print(publish(a.directory))
