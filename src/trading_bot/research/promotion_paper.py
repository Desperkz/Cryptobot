"""Prospective public-mainnet paper transfer of demo-shadow conditional buckets."""
import json,time
from dataclasses import replace
from decimal import Decimal

from trading_bot.bot import _shadow_conditional_lab_variant,_shadow_conditional_lab_v2_variant
from trading_bot.research.early_paper import EarlyStore,finite
from trading_bot.research.mainnet_paper import Lab,MINUTE,canonical,main
from trading_bot.research.confirmation_paper import _valid_frames
from trading_bot.research.cost_sizing import compare_sizing
from trading_bot.strategy_engine.router import _as_squeeze_dynamic_variant
from trading_bot.strategy_engine.squeeze_breakout import SqueezeBreakoutStrategy

ARMS=('PROMOTE_SOURCE_2R','PROMOTE_V1_HIGH_2R','PROMOTE_V1_MID_2R','PROMOTE_C2_MID_2R')
RULE={'name':'sqz-promotion-transfer-v1',
    'scope':'Public-mainnet transfer test of demo-shadow UPD HIGH, MID and C2 MID; common 2R execution and paired cost sizing',
    'source':'Unchanged SQZ generator wrapped as SQUEEZE_BREAKOUT_DYNAMIC_UPD; original fixed 32 symbols/liquidity floors',
    'selection':'Existing unchanged conditional v1 HIGH/MID and v2 MID buckets; C2 HIGH stays shadow until original 50-trade review',
    'annotations':'Original legacy OF for saved score rules, corrected OF and RS retained; no new direction/confirmation gates',
    'timing':'Actual closed hour, first observation only, no retry/backdating; shared source occupancy across all arms',
    'execution':'All arms use identical next-minute entry, original stop, single 2R, 24h and 2 virtual USDT stop risk before costs',
    'cost_comparison':'Same source/bars/fills/targets, stop-only versus stop+4bps fees+exit slippage+24h nonnegative funding reserve; 5/10bps adverse per-side stress',
    'risk_limit':'Estimated ordinary-stop budget, not a guarantee against gaps, changing funding or real fills; no quantity rounding or leverage in this virtual model',
    'review':'At least 50 closed prospective source-control entries and 14 days; formal candidate review additionally requires 50 closed entries in that arm; report opens and 24h-aged sources',
    'promotion':'Demo evidence and a dashboard badge do not validate mainnet profitability; no main strategy mode/allowlist change or real orders',
    'operation':'Resource-guarded serialized --once cycles on a 300s systemd timer; scan/monitor once per cycle; release memory between cycles',
    'no_refit':'Keep historical data diagnostic; do not choose bucket thresholds/exits/coins from replay PnL'}


class TransferStrategy(SqueezeBreakoutStrategy):
    def generate(self,symbol,candles_15m,candles_1h,candles_4h,metrics):
        signal=super().generate(symbol,candles_15m,candles_1h,candles_4h,metrics)
        if signal is None:return None
        observed=int(time.time()*1000)
        good=all(_valid_frames(cs,d,observed) for cs,d in ((candles_15m,900000),(candles_1h,3600000),(candles_4h,14400000)))
        signal=_as_squeeze_dynamic_variant(signal,'SQUEEZE_BREAKOUT_DYNAMIC_UPD')
        return replace(signal,metadata={**signal.metadata,'transfer_closed_frames_valid':good})


def transfer_decisions(signal,config):
    invalid={'allowed':{a:False for a in ARMS},'source_quality_failures':['INVALID_TRANSFER_SOURCE'],'rule_version':RULE['name']}
    m=signal.metadata
    if not isinstance(m,dict) or m.get('strategy')!='SQUEEZE_BREAKOUT_DYNAMIC_UPD' or m.get('transfer_closed_frames_valid') is not True:return invalid
    try:
        if signal.direction.value not in ('LONG','SHORT'):return invalid
        entry,stop=finite(signal.entry_price),finite(signal.stop_loss)
        if entry is None or stop is None or min(entry,stop)<=0 or (entry-stop)*(1 if signal.direction.value=='LONG' else -1)<=0:return invalid
        if any(finite(m.get(k)) is None for k in ('breakout_atr','volume_ratio')):return invalid
        for key in ('order_flow','p8_order_flow'):
            f=m.get(key);score=finite(f.get('score')) if isinstance(f,dict) else None
            if not isinstance(f,dict) or f.get('alignment') not in ('aligned','mixed','against') or score is None or not Decimal('0')<=score<=Decimal('1'):return invalid
            if not all(isinstance(f.get(k),list) and all(isinstance(x,str) for x in f[k]) for k in ('reasons','risk_flags')):return invalid
        _,v1=_shadow_conditional_lab_variant(signal,config);_,v2=_shadow_conditional_lab_v2_variant(signal,config)
        if v1 is None or v2 is None:return invalid
        return {'allowed':dict(zip(ARMS,(True,v1['bucket']=='HIGH',v1['bucket']=='MID',v2['bucket']=='MID'))),
                'source_quality_failures':[],'conditional_v1':v1,'conditional_v2':v2,'rule_version':RULE['name']}
    except (ValueError,TypeError,ArithmeticError,AttributeError):return invalid


class TransferStore(EarlyStore):
    def __init__(self,directory,manifest):
        super().__init__(directory,{**manifest,'scan_interval_sec':300,'monitor_interval_sec':300,
                                    'operation_mode':'RESOURCE_GUARDED_PERIODIC_ONESHOT'})
    def source_id(self,signal):return super().source_id(signal).replace('SQZ_EARLY:','SQZ_TRANSFER:',1)


class TransferLab(Lab):
    arms=ARMS
    def __init__(self,config,symbols,store,cohort):
        super().__init__(config,symbols,store,cohort)
        self.strategy=TransferStrategy(config.strategy,self.strategy.regime_detector)
    def accepts(self,signal):return signal.metadata.get('strategy')=='SQUEEZE_BREAKOUT_DYNAMIC_UPD'
    def decisions(self,signal):return transfer_decisions(signal,self.gate_config)

    async def monitor(self,client):
        await super().monitor(client)
        now=int(time.time()*1000);errors=[];checked=0
        pending=self.store.db.execute('''SELECT * FROM positions WHERE result IS NOT NULL AND (
            status!='CLOSED' OR json_extract(result,'$.cost_comparison."10bps".stop_only.status') IS NULL
            OR (json_extract(result,'$.cost_comparison."10bps".stop_only.status')='OPEN'
                AND coalesce(json_extract(result,'$.comparison_complete'),0)=0))
            ORDER BY id LIMIT 256''').fetchall()
        for row in pending:
            result=json.loads(row['result'])
            previous=result.get('cost_comparison')
            if row['status']=='CLOSED' and previous and (previous['10bps']['stop_only']['status']!='OPEN' or now>=row['entry_ms']+1440*MINUTE and result.get('comparison_complete')):continue
            if 'R' not in result:continue
            try:
                plan=json.loads(row['plan']);end=min(now//MINUTE*MINUTE,row['entry_ms']+1440*MINUTE)
                bars=[json.loads(r[0]) for r in self.store.db.execute('SELECT payload FROM execution_candles WHERE symbol=? AND open_ms>=? AND open_ms<? ORDER BY open_ms',(row['symbol'],row['entry_ms'],row['entry_ms']+1440*MINUTE))]
                # A stress target may close later; retain coverage beyond the original exit.
                if row['status']=='CLOSED':
                    cursor=row['entry_ms']
                    for b in bars:
                        if int(b[0])!=cursor:break
                        cursor+=MINUTE
                    while cursor<end:
                        raw=await client.klines(row['symbol'],'1m',limit=1000,start_time=cursor,end_time=end-1)
                        closed=[b for b in raw if cursor<=int(b[0])<end and int(b[6])<now]
                        if not closed:break
                        with self.store.db:self.store.db.executemany('INSERT OR IGNORE INTO execution_candles VALUES(?,?,?)',[(row['symbol'],int(b[0]),canonical(b)) for b in closed])
                        cursor=int(closed[-1][0])+MINUTE
                    bars=[json.loads(r[0]) for r in self.store.db.execute('SELECT payload FROM execution_candles WHERE symbol=? AND open_ms>=? AND open_ms<? ORDER BY open_ms',(row['symbol'],row['entry_ms'],row['entry_ms']+1440*MINUTE))]
                comparison=compare_sizing(bars,float(plan['stop']),row['direction'],plan['targets'],complete=len(bars)==1440,funding_rate=plan['funding_rate'],budget=plan['risk_usdt'])
                baseline=comparison['5bps']['stop_only']
                if abs(baseline['R']-result['R'])>1e-8:raise ValueError('Original execution replay mismatch')
                result.update(cost_comparison=comparison,comparison_complete=len(bars)==1440,comparison_last_bar_ms=bars[-1][6]);checked+=1
                with self.store.db:self.store.db.execute('UPDATE positions SET result=? WHERE id=?',(canonical(result),row['id']))
            except Exception as exc:errors.append({'position':row['id'],'error':str(exc)[:300]})
            finally:client.evidence.clear()
        client.evidence.clear()
        self.store.set('cost_monitor',{'at_ms':now,'status':'OK' if not errors else 'DEGRADED','checked':checked,'errors':errors})
        status=self.store.status();tmp=self.store.directory/'status.tmp';tmp.write_text(json.dumps(status,indent=2));tmp.replace(self.store.directory/'status.json')


def periodic_main():
    import sys
    if '--once' not in sys.argv:raise ValueError('This resource-limited pilot requires periodic --once execution')
    main(lab_class=TransferLab,store_class=TransferStore,experiment=RULE)


if __name__=='__main__':periodic_main()
