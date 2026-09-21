"""Local, versioned structure plans. Signal freshness is independent of plan lifetime."""
from copy import deepcopy
from datetime import datetime,time,timedelta
from hashlib import sha256
from zoneinfo import ZoneInfo
from .models import stamp,symbol_market
from .calendars import local_date
from .plan_rules import execution_range

STATES={'observation':'观察','waiting_trigger':'等待触发','execution_check':'执行核验','buyable':'当前可买',
        'bought':'已买入','invalid':'已失效','expired':'已过期','recovering':'待恢复'}

class TradePlans:
    def __init__(self,store):
        self.store=store
        self.rows={p['id']:p for market in ('CN','US') for p in store.research_list('plan',market,limit=500)}
        self.verified=set()
        for row in self.rows.values():
            if row['state'] not in ('bought','invalid','expired'):
                row.update(state='recovering',reason='服务已恢复，等待本轮重新核验')

    def save(self,row):
        before=self.rows.get(row['id'])
        if before==row:return row
        row=deepcopy(row);row['revision']=(before or {}).get('revision',0)+1
        self.rows[row['id']]=row
        self.store.research_save('plan',row['id'],row)
        self.store.research_save('plan_history',f"{row['id']}:{row['revision']}",row)
        return row

    def observe(self,signal,meta,cfg,t):
        market=symbol_market(signal.symbol);day=str(local_date(signal.time,market))
        structure=signal.evidence.get('structure_id') or sha256(f'{signal.symbol}|{signal.strategy}|{signal.version}|{day}'.encode()).hexdigest()[:24]
        key=sha256(f'{structure}|{day}'.encode()).hexdigest()[:24]
        old=self.rows.get(key)
        if old and old['state'] in ('bought','invalid','expired'):return old
        if old and old.get('signal_id')==signal.id:
            self.verified.add(key);return old
        tz=ZoneInfo('Asia/Shanghai' if market=='CN' else 'America/New_York')
        deadline=stamp(signal.evidence.get('entry_deadline') or datetime.combine(local_date(t,market),time(14,45),tz).isoformat())
        priced=execution_range(signal,cfg)
        row={**(old or {}),'id':key,'structure_id':structure,'symbol':signal.symbol,'market':market,'date':day,
             'name':meta.get('name',signal.symbol),'strategy':signal.strategy,'strategy_version':signal.version,
             'source':signal.source,'trial':True,'evidence_label':'试运行，尚未验证胜率',
             'created_at':(old or {}).get('created_at',t.isoformat()),'updated_at':t.isoformat(),
             'signal_id':signal.id,'signal_time':signal.time.isoformat(),'valid_until':deadline.isoformat(),
             'signal_valid_until':(signal.time+timedelta(minutes=signal.evidence.get('signal_minutes',5))).isoformat(),
             'entry_min':priced['entry_min'],'entry_max':priced['entry_max'],'stop':priced['stop'],
             'target':priced['target'],'net_rr':priced.get('net_rr'),'executable_range':priced['executable'],
             'horizon':signal.evidence.get('horizon','short'),'max_hold_sessions':signal.evidence.get('max_hold_sessions',3),
             'exit_policy':signal.evidence.get('exit_policy',{}),'evidence':deepcopy(signal.evidence),'no_chase':priced.get('no_chase'),'quote_max':priced.get('quote_max'),
             'reason':priced['reason'] if not priced['executable'] else '形态已触发，等待同源实时行情、盘口与风险检查',
             'state':'execution_check'}
        if t>=deadline:row.update(state='expired',reason='已超过当日14:45计划有效期')
        self.verified.add(key)
        return self.save(row)

    def update(self,decisions,selection,context,t):
        by_signal={d['signal_id']:d for d in decisions};pool={r['symbol']:r for r in selection}
        for key,old in list(self.rows.items()):
            if old['state'] in ('bought','invalid','expired'):continue
            row=deepcopy(old);s=row['symbol'];meta=pool.get(s)
            if t>=stamp(row['valid_until']):row.update(state='expired',reason='当日结构计划已到期，等待新的计划')
            elif meta and (meta.get('decision')=='暂不参与' or meta.get('setup_stage')=='invalid'):
                row.update(state='invalid',reason=meta.get('reason','结构失效'))
            elif key not in self.verified or not meta or meta.get('data_quality')=='missing':
                row.update(state='recovering',reason='等待恢复后重新验证结构及实时行情')
            else:
                gate=context.get(s,{});decision=by_signal.get(row['signal_id'])
                row['checks']=gate.get('checks',[])
                if t>=stamp(row['signal_valid_until']):row.update(state='waiting_trigger',reason='上一买点已过期，等待新完整K线重新触发')
                elif not gate.get('passed',False) and row['market']=='CN':
                    row.update(state='execution_check',reason=gate.get('reason','市场、板块或公告条件待核验'))
                elif decision:
                    row.update(state='buyable' if decision['state']=='buy' else 'execution_check',reason=decision['reason'])
                    for field in ('qty','price','quote_time','depth_time','current_price','net_rr'):
                        if field in decision:row[field]=decision[field]
                else:row.update(state='execution_check',reason='等待实时监测名额和执行检查')
            # Price timestamps do not create an unbounded revision every five seconds.
            important=('state','reason','signal_id','checks')
            if any(row.get(k)!=old.get(k) for k in important):
                row['updated_at']=t.isoformat();self.save(row)
            else:self.rows[key]=row

    def list(self,market='CN',include_history=False):
        rows=[deepcopy(r) for r in self.rows.values() if r['market']==market]
        if not include_history:rows=[r for r in rows if r['state'] not in ('invalid','expired')]
        for row in rows:row['state_label']=STATES.get(row['state'],row['state'])
        order={s:i for i,s in enumerate(('buyable','execution_check','waiting_trigger','recovering','observation','bought','invalid','expired'))}
        return sorted(rows,key=lambda r:(order.get(r['state'],99),r['updated_at']),reverse=False)

    def confirm_buy(self,key,body,real,t):
        row=self.rows.get(key)
        if not row:raise ValueError('计划不存在')
        # Confirmation records an already executed human trade, even if it departed from the plan.
        holding_id='plan:'+key
        existing=next((h for h in real.list() if h['id']==holding_id),None)
        if existing:
            if float(body['cost'])!=existing['cost'] or float(body['quantity'])!=existing.get('initial_quantity',existing['quantity']):
                raise ValueError('该计划已经登记过实际买入；请在持仓中修改记录')
            return existing
        data={**body,'id':holding_id,'symbol':row['symbol'],'name':row['name'],'plan_id':key,
              'strategy':row['strategy'],'strategy_version':row['strategy_version'],
              'original_plan':deepcopy(row),'stop':row['stop'],'target':row['target']}
        holding=real.upsert_manual(data)
        self.save({**row,'state':'bought','updated_at':t.isoformat(),'holding_id':holding['id'],
                   'reason':'用户已手动确认实际买入，持续跟踪退出条件'})
        return holding
