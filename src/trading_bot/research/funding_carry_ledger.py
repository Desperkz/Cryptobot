"""Cash and collateral ledger for an isolated matched spot/perp SHADOW study.

No exchange/order/database methods. Fees are quote-equivalent model costs.
"""
from __future__ import annotations
import math
from collections import defaultdict

def finite(value, positive=False):
 if isinstance(value,bool):raise ValueError("INVALID_BOOLEAN_NUMBER")
 value=float(value)
 if not math.isfinite(value) or (positive and value<=0):raise ValueError("INVALID_FINITE_NUMBER")
 return value

class CarryLedger:
 def __init__(self,capital,spot_fee=.001,perp_fee=.0004,slip=.0005):
  capital=finite(capital,True)
  for value in (spot_fee,perp_fee,slip):
   if not 0<=finite(value)<.1:raise ValueError("INVALID_COST_RATE")
  self.capital=capital;self.cash=capital;self.sf=spot_fee;self.pf=perp_fee;self.slip=slip
  self.positions={};self.flows=defaultdict(float);self.funding=defaultdict(float)
  self.fees=0.;self.slippage=0.;self.events=[]
 def qty(self,symbol):return self.positions.get(symbol,{}).get('qty',0.)
 def trade(self,symbol,qty,spot,perp,at):
  qty=finite(qty);spot=finite(spot,True);perp=finite(perp,True)
  if qty<0 or not isinstance(at,int) or at<0:raise ValueError("INVALID_QUANTITY_OR_TIME")
  old=self.positions.get(symbol,{'qty':0.,'average':0.});delta=qty-old['qty']
  if abs(delta)<1e-12:return
  sign=1 if delta>0 else -1;sf=spot*(1+sign*self.slip);pf=perp*(1-sign*self.slip)
  spotfee=abs(delta)*sf*self.sf;perpfee=abs(delta)*pf*self.pf
  realized=(-delta)*(old['average']-pf) if delta<0 else 0.
  flow=-delta*sf-spotfee+realized-perpfee
  self.cash+=flow;self.flows[symbol]+=flow;self.fees+=spotfee+perpfee
  self.slippage+=abs(delta)*(abs(sf-spot)+abs(pf-perp))
  average=(old['qty']*old['average']+delta*pf)/qty if delta>0 else old['average'] if qty>0 else 0.
  self.positions[symbol]={'qty':qty,'average':average}
  self.events.append({'at_ms':at,'symbol':symbol,'previous_qty':old['qty'],'new_qty':qty,'spot_mid':spot,'perp_mid':perp,
   'spot_fill':sf,'perp_fill':pf,'spot_fee':spotfee,'perp_fee':perpfee,'perp_realized':realized,'cash_flow':flow,'cash_after':self.cash})
  assert qty>=0 and self.cash>-1e-7,'Cash borrowing is not allowed'
 def settlement(self,symbol,rate,mark,at):
  rate=finite(rate);mark=finite(mark,True)
  value=self.qty(symbol)*mark*rate;self.cash+=value;self.flows[symbol]+=value;self.funding[symbol]+=value
  return value
 def liquidation(self,prices):
  equity=self.cash;contribution=dict(self.flows);closing_fees=0.;closing_slip=0.
  for s,pos in self.positions.items():
   q=pos['qty']
   if not q:continue
   if s not in prices:return None,None,None,None
   spot,perp=prices[s];sf=spot*(1-self.slip);pf=perp*(1+self.slip)
   fee=q*(sf*self.sf+pf*self.pf);value=q*sf+q*(pos['average']-pf)-fee
   equity+=value;contribution[s]=contribution.get(s,0)+value
   closing_fees+=fee;closing_slip+=q*((spot-sf)+(pf-perp))
  return equity,contribution,self.fees+closing_fees,self.slippage+closing_slip
 def margin_surplus(self,perp_highs,maintenance=.01):
  balance=self.cash;required=0.
  for s,pos in self.positions.items():
   q=pos['qty'];price=perp_highs[s]
   balance+=q*(pos['average']-price);required+=q*price*maintenance
  return balance-required

 def snapshot(self):
  return {"capital":self.capital,"cash":self.cash,"spot_fee":self.sf,"perp_fee":self.pf,"slip":self.slip,
          "positions":self.positions,"flows":dict(self.flows),"funding":dict(self.funding),"fees":self.fees,"slippage":self.slippage}
 @classmethod
 def restore(cls,raw):
  book=cls(raw["capital"],raw["spot_fee"],raw["perp_fee"],raw["slip"])
  book.cash=finite(raw["cash"]);book.fees=finite(raw["fees"]);book.slippage=finite(raw["slippage"])
  book.positions={s:{"qty":finite(p["qty"]),"average":finite(p["average"])} for s,p in raw["positions"].items()}
  if any(p["qty"]<0 or p["average"]<0 for p in book.positions.values()):raise ValueError("INVALID_SAVED_POSITION")
  book.flows=defaultdict(float,{s:finite(v) for s,v in raw["flows"].items()})
  book.funding=defaultdict(float,{s:finite(v) for s,v in raw["funding"].items()})
  return book
