from datetime import timedelta
from app.models import Quote,Bar,stamp
from app.research_context import filing_screen,daily_sector_snapshot,context_gate

T=stamp('2026-09-18T02:00:00Z')
def filing(title='董事会决议',age=3):
    return {'id':'1','title':title,'publish_at':(T-timedelta(days=age)).isoformat(),'file_urls':['https://example.com/notice.pdf']}
def test_directory_gaps_and_material_risks_never_become_clear():
    assert filing_screen([],T)['state']=='missing'
    assert filing_screen([filing('重大诉讼进展')],T)['state']=='pending_review'
    assert filing_screen([filing()],T)['state']=='checked'
    assert filing_screen([filing() for _ in range(100)],T)['state']=='missing'
    assert filing_screen([{'title':'缺时间'}],T)['state']=='missing'

def test_sector_rank_requires_scope_and_history_coverage():
    universe=[{'symbol':f'60000{i}.SH','industry':'行业'} for i in range(6)]
    bars=[{'close':10+i/100} for i in range(65)]
    cache={r['symbol']:{'cutoff':'2026-09-17','bars':bars} for r in universe[:4]}
    benchmark=[Bar('000300.SH','longbridge',T-timedelta(days=70-i),100,101,99,100,1000) for i in range(65)]
    snap=daily_sector_snapshot(universe,cache,benchmark,'2026-09-17')
    assert snap['sectors']['行业']['rs5'] is None and snap['rank_coverage']==0
    cache[universe[4]['symbol']]={'cutoff':'2026-09-17','bars':bars}
    snap=daily_sector_snapshot(universe,cache,benchmark,'2026-09-17')
    assert snap['sectors']['行业']['rs5']>0 and snap['rank_coverage']==1

def test_chain_requires_fresh_breadth_sector_and_events():
    symbols=[f'60000{i}.SH' for i in range(5)]
    quotes={s:Quote(s,'longbridge',s,10,T,T,change_pct=1,quality='realtime') for s in symbols}
    snapshot={'supported':5,'cutoff_current':True,'benchmark_trend':True,'rank_coverage':1,
              'sectors':{'行业':{'symbols':symbols,'rs3':.03,'rs5':.05,'rank_percentile':20,'history_coverage':1}}}
    bars=[Bar('000300.SH','longbridge',T-timedelta(minutes=10-i*5),100,101+i,99,100+i,1000) for i in range(2)]
    row={'industry':'行业','name_verified':True,'data_quality':'complete'}
    event=filing_screen([filing()],T)
    assert context_gate(row,snapshot,quotes,bars,event,T)['passed']
    assert not context_gate(row,snapshot,quotes,bars,event,T+timedelta(minutes=7))['passed']
    assert not context_gate(row,snapshot,quotes,bars,{'state':'missing'},T)['passed']
