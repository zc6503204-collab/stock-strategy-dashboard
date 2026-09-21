"""Auditable market, sector and event gates using timestamped public data."""
from collections import defaultdict
from datetime import timedelta
import re
from .models import stamp, in_scope
from .calendars import local_date


def fresh(q,t,seconds=300):
    return bool(q and q.market_time and q.quality=='realtime' and
                0 <= (t-q.market_time).total_seconds() <= seconds and
                0 <= (t-q.received_at).total_seconds() <= seconds)


def daily_sector_snapshot(universe,caches,benchmark,cutoff):
    """Classification is today's snapshot, never advertised as historical membership."""
    groups=defaultdict(list)
    for r in universe:
        if in_scope(r['symbol']):groups[str(r.get('industry') or '未知行业')].append(r['symbol'])
    sectors={}
    base={}
    if len(benchmark)>=25:
        base={n:benchmark[-1].close/benchmark[-n-1].close-1 for n in (3,5)}
    for name,symbols in groups.items():
        values=[]
        for symbol in symbols:
            cache=caches.get(symbol,{})
            bars=cache.get('bars',[])
            if cache.get('cutoff')!=cutoff or len(bars)<65:continue
            if not all(bars[-n-1].get('close',0)>0 for n in (3,5)):continue
            values.append({n:bars[-1]['close']/bars[-n-1]['close']-1 for n in (3,5)})
        coverage=len(values)/len(symbols) if symbols else 0
        row={'industry':name,'symbols':symbols,'members':len(symbols),'checked':len(values),
             'history_coverage':coverage,'cutoff':cutoff,'classification_date':None,
             'rs3':None,'rs5':None,'rank_percentile':None}
        if len(values)>=5 and coverage>=.8 and base and name!='未知行业':
            row.update(rs3=sum(v[3] for v in values)/len(values)-base[3],
                       rs5=sum(v[5] for v in values)/len(values)-base[5])
        sectors[name]=row
    ranked=sorted((s for s in sectors.values() if s['rs5'] is not None),key=lambda s:s['rs5'],reverse=True)
    for i,row in enumerate(ranked):row['rank_percentile']=i/max(1,len(ranked)-1)*100
    return {'sectors':sectors,'rank_coverage':len(ranked)/len(sectors) if sectors else 0,
            'benchmark_trend':bool(base and benchmark[-1].close>sum(b.close for b in benchmark[-20:])/20>
                                  sum(b.close for b in benchmark[-25:-5])/20),'cutoff':cutoff}


def shared_context(snapshot,quotes,benchmark_intraday,t):
    supported=snapshot.get('supported',0)
    valid=[q for q in quotes.values() if fresh(q,t) and q.change_pct is not None]
    quote_coverage=len(valid)/supported if supported else 0
    breadth=sum(q.change_pct>0 for q in valid)/len(valid) if valid else 0
    bb=[b for b in benchmark_intraday if local_date(b.start,'CN')==local_date(t,'CN') and b.final and b.end<=t]
    volume=sum(b.volume for b in bb)
    vwap=sum((b.high+b.low+b.close)/3*b.volume for b in bb)/volume if volume else None
    benchmark_fresh=bool(bb and 0 <= (t-bb[-1].end).total_seconds() <= 360)
    market_data=quote_coverage>=.8 and benchmark_fresh and snapshot.get('cutoff_current',False)
    market_ok=bool(market_data and snapshot.get('benchmark_trend') and vwap and bb[-1].close>vwap and breadth>=.5)
    sector_coverage={}
    for name,sector in snapshot.get('sectors',{}).items():
        members=sector.get('symbols',[])
        sector_coverage[name]=sum(fresh(quotes.get(s),t) for s in members)/len(members) if members else 0
    return {'quote_coverage':quote_coverage,'breadth':breadth,'market_data':market_data,'market_ok':market_ok,'sector_coverage':sector_coverage}


def context_gate(row,snapshot,quotes,benchmark_intraday,event,t,shared=None):
    shared=shared if shared is not None else shared_context(snapshot,quotes,benchmark_intraday,t)
    quote_coverage=shared['quote_coverage'];breadth=shared['breadth'];market_data=shared['market_data'];market_ok=shared['market_ok']
    sector=snapshot.get('sectors',{}).get(str(row.get('industry') or '未知行业'),{})
    members=sector.get('symbols',[])
    sector_quotes=shared['sector_coverage'].get(str(row.get('industry') or '未知行业'),0)
    sector_data=bool(market_data and sector.get('rs5') is not None and sector_quotes>=.8 and snapshot.get('rank_coverage',0)>=.8)
    sector_ok=bool(sector_data and sector['rs3']>0 and sector['rs5']>0 and sector['rank_percentile']<=30)
    event_fresh=bool(event.get('checked_at') and 0 <= (t-stamp(event['checked_at'])).total_seconds() <= 1800)
    event_ok=event_fresh and event.get('state')=='checked'
    checks=[{'layer':'市场','passed':market_ok,'data_complete':market_data,
             'reason':'指数趋势、VWAP与上涨占比通过' if market_ok else '市场数据覆盖不足' if not market_data else '指数趋势、VWAP或上涨占比未通过',
             'quote_coverage':quote_coverage,'advancing_fraction':breadth},
            {'layer':'板块','passed':sector_ok,'data_complete':sector_data,
             'reason':'行业3/5日相对强度与排名通过' if sector_ok else '行业历史、报价或横向排名覆盖不足' if not sector_data else '行业持续性或前30%排名未通过',
             'industry':row.get('industry'),'history_coverage':sector.get('history_coverage',0),
             'quote_coverage':sector_quotes,'rs3':sector.get('rs3'),'rs5':sector.get('rs5'),'rank_percentile':sector.get('rank_percentile')},
            {'layer':'个股','passed':bool(row.get('name_verified') and row.get('data_quality')=='complete' and row.get('decision')!='暂不参与'),
             'reason':row.get('reason','证券身份与流动性待核验')},
            {'layer':'公告风险','passed':bool(event_ok),'data_complete':bool(event_fresh and event.get('state')!='missing'),
             'reason':event.get('reason','公告目录尚未核验') if event_fresh else '公告核验已过期或尚未完成',
             'checked_at':event.get('checked_at'),'items':event.get('items',[]),'method':'公告目录规则筛查；不代表全文尽调'}]
    blocked=[c['reason'] for c in checks if not c['passed']]
    return {'passed':not blocked,'reason':'；'.join(blocked) if blocked else '市场、板块、个股和公告目录检查通过',
            'checks':checks,'checked_at':t.isoformat(),'classification_date':snapshot.get('classification_date'),
            'rating':'试运行条件检查，非胜率或完整A股评级'}


MATERIAL=re.compile(r'立案|调查|处罚|退市|风险警示|停牌|复牌|减持|质押|冻结|诉讼|仲裁|担保|违约|亏损|预亏|业绩预告|业绩修正|重组|收购|合并|分红|派息|除权|转增|配股|发行.*股|重大|异常波动')

def filing_screen(rows,t):
    """Never turn a failed, malformed or truncated directory into an all-clear."""
    result={'checked_at':t.isoformat(),'source':'longbridge','state':'missing','items':[],
            'reason':'公告目录不足以完成风险筛查','lookback_days':30}
    if not isinstance(rows,list) or not rows:return result
    valid=[]
    for r in rows:
        try:published=stamp(r['publish_at'])
        except (ValueError,TypeError,KeyError):return result
        if not r.get('id') or not r.get('title') or published>t:return result
        valid.append((published,r))
    cutoff=t-timedelta(days=30)
    if len(rows)>=100 and min(x[0] for x in valid)>cutoff:
        result['reason']='公告目录截断，近30日覆盖未完成';return result
    recent=[r for p,r in valid if p>=cutoff]
    risky=[r for r in recent if MATERIAL.search(r['title'])]
    result.update(state='pending_review' if risky else 'checked',
                  reason='存在重大事项公告待核验，暂停买入' if risky else '近30日公告目录未命中风险规则；不代表全文尽调',
                  items=[{'id':r['id'],'title':r['title'],'publish_at':r['publish_at'],'urls':r.get('file_urls',[])} for r in risky],
                  checked_count=len(recent))
    return result
