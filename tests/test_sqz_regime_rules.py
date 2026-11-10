import pytest
from trading_bot.research.sqz_regime_rules import features,decide,structure_stop,HOUR

def bars(n=530):
 return [[i*HOUR,100+i,101+i,99+i,100+i,1,i*HOUR+HOUR-1] for i in range(n)]

def test_features_do_not_change_when_future_extreme_is_appended():
 raw=bars();before=features(raw);later=raw+[[530*HOUR,9000,10000,1,9500,1,531*HOUR-1]]
 assert features(later)[:530]==before
 assert before[-1]['available_ms']==530*HOUR

def test_hourly_gaps_rejected():
 raw=bars();del raw[100]
 with pytest.raises(ValueError,match='GAPPED'):features(raw)

def test_gate_distinguishes_long_short_and_warmup():
 f=features(bars())[-1]
 assert decide(f,f,'LONG','BTCUSDT')['accepted']
 assert not decide(f,f,'SHORT','BTCUSDT')['accepted']
 assert decide(features(bars(80))[-1],f,'LONG','BTCUSDT')['reasons']==['WARMUP']

def test_relative_weak_coin_rejected_even_with_positive_trend():
 f=features(bars())[-1];weak={**f,'return24':f['return24']-.02}
 assert 'RELATIVE_STRENGTH' in decide(weak,f,'LONG','ETHUSDT')['reasons']

def test_structure_never_loosens_protection():
 raw=bars(5)
 assert structure_stop(raw,'LONG',101)==101
 assert structure_stop(raw,'LONG',90)==99
 assert structure_stop(raw,'SHORT',90)==90
 assert structure_stop(raw,'SHORT',110)==105

def test_five_closed_bars_are_required():
 with pytest.raises(ValueError):structure_stop(bars(4),'LONG',90)
