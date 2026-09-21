import sqlite3,json,threading
from pathlib import Path
from .models import now

class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path,check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS bars(symbol TEXT,source TEXT,time TEXT,body TEXT NOT NULL, PRIMARY KEY(symbol,source,time));
        CREATE TABLE IF NOT EXISTS signals(id TEXT PRIMARY KEY,time TEXT,body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,time TEXT,kind TEXT,body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS benchmarks(id INTEGER PRIMARY KEY,time TEXT,source TEXT,body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS research_records(kind TEXT,id TEXT,market TEXT,date TEXT,symbol TEXT,time TEXT,body TEXT NOT NULL,PRIMARY KEY(kind,id));
        CREATE INDEX IF NOT EXISTS research_record_lookup ON research_records(kind,market,date,symbol,time);
        ''')

    def get(self,key,default=None):
        with self.lock:
            row=self.db.execute('SELECT value FROM kv WHERE key=?',(key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set(self,key,value):
        with self.lock,self.db:
            self.db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)',(key,json.dumps(value,ensure_ascii=False)))

    def bar(self,bar):
        with self.lock,self.db:
            # Original observed bar remains immutable. A vendor correction cannot rewrite a decision.
            self.db.execute('INSERT OR IGNORE INTO bars VALUES (?,?,?,?)',(bar.symbol,bar.source,bar.start.isoformat(),json.dumps(bar.dump())))

    def bars(self,symbol,source,limit=5000):
        with self.lock:
            rows=self.db.execute('SELECT body FROM bars WHERE symbol=? AND source=? ORDER BY time DESC LIMIT ?',(symbol,source,limit)).fetchall()
            return [json.loads(r[0]) for r in rows[::-1]]

    def signal(self,s):
        with self.lock,self.db:
            cursor=self.db.execute('INSERT OR IGNORE INTO signals VALUES (?,?,?)',(s.id,s.time.isoformat(),json.dumps(s.dump(),ensure_ascii=False)))
            return cursor.rowcount>0

    def signals(self):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute('SELECT body FROM signals ORDER BY time DESC LIMIT 300')]

    def event(self,kind,body):
        with self.lock,self.db:
            self.db.execute('INSERT INTO events(time,kind,body) VALUES (?,?,?)',(now().isoformat(),kind,json.dumps(body,ensure_ascii=False)))

    def events(self,limit=80):
        with self.lock:
            return [dict(time=t,kind=k,**json.loads(b)) for t,k,b in self.db.execute('SELECT time,kind,body FROM events ORDER BY id DESC LIMIT ?',(limit,))]

    def benchmark(self,source,body):
        with self.lock,self.db:
            self.db.execute('INSERT INTO benchmarks(time,source,body) VALUES (?,?,?)',(now().isoformat(),source,json.dumps(body)))

    def benchmarks(self):
        with self.lock:
            return [dict(time=t,source=s,**json.loads(b)) for t,s,b in self.db.execute('SELECT time,source,body FROM benchmarks ORDER BY id DESC LIMIT 300')]

    def research_save(self,kind,key,body):
        with self.lock,self.db:
            self.db.execute('INSERT OR REPLACE INTO research_records VALUES (?,?,?,?,?,?,?)',
                (kind,key,body.get('market','CN'),body.get('date'),body.get('symbol'),now().isoformat(),json.dumps(body,ensure_ascii=False)))

    def research_get(self,kind,key):
        with self.lock:
            row=self.db.execute('SELECT body FROM research_records WHERE kind=? AND id=?',(kind,key)).fetchone()
        return json.loads(row[0]) if row else None

    def research_list(self,kind,market='CN',day=None,symbol=None,limit=100):
        with self.lock:
            rows=self.db.execute('SELECT body FROM research_records WHERE kind=? AND market=? AND (? IS NULL OR date=?) AND (? IS NULL OR symbol=?) ORDER BY time DESC LIMIT ?',
                                 (kind,market,day,day,symbol,symbol,max(1,min(limit,500)))).fetchall()
        return [json.loads(r[0]) for r in rows]

    def daily_cache_symbols(self,cutoff):
        with self.lock:
            return [r[0].split(':',1)[1] for r in self.db.execute("SELECT key FROM kv WHERE key LIKE 'selection_daily:%' AND json_extract(value,'$.cutoff')=?",(cutoff,))]

    def all_signals(self,market=None):
        from .models import symbol_market
        with self.lock:rows=[json.loads(r[0]) for r in self.db.execute('SELECT body FROM signals ORDER BY time')]
        return [r for r in rows if not market or symbol_market(r['symbol'])==market]

    def research_funnel(self,market,day):
        from .models import stamp,symbol_market
        from .calendars import local_date
        from collections import defaultdict
        signals=[r for r in self.all_signals(market) if str(local_date(stamp(r['time']),market))==day]
        ids={r['id'] for r in signals};reasons=defaultdict(set);states={};buy=set()
        with self.lock:
            rows=self.db.execute("SELECT body FROM events WHERE kind='decision' ORDER BY id").fetchall()
        for raw, in rows:
            r=json.loads(raw);sid=r.get('signal_id')
            if sid not in ids:continue
            reasons[r.get('reason','未记录')].add(sid);states[sid]=r.get('state')
            if r.get('state')=='buy':buy.add(sid)
        return {'signals':len(ids),'execution_passed':len(buy),'unrecorded':len(ids-set(states)),
                'last_states':dict(__import__('collections').Counter(states.values())),
                'reasons':[{'reason':k,'signals':len(v)} for k,v in sorted(reasons.items(),key=lambda x:-len(x[1]))],
                'reason_counts_overlap':True}
