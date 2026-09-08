"""Persistent local notifications. No model, Codex, or remote notification service."""
import asyncio,json,sys
from pathlib import Path
from .models import now

DEFAULTS={'enabled':True,'desktop':True,'sound':True}
class Alerts:
    def __init__(self,store,root):
        self.store=store;self.root=Path(root);self.settings={**DEFAULTS,**store.get('notification_settings',{})}
        self.permission='unknown';self.queue=asyncio.Queue();self.last_error=None
        with store.lock,store.db:
            store.db.execute('CREATE TABLE IF NOT EXISTS alerts (id TEXT PRIMARY KEY,time TEXT,body TEXT,read INTEGER DEFAULT 0,delivery TEXT DEFAULT "pending")')
    def list(self,limit=100,before=None):
        with self.store.lock:
            rows=self.store.db.execute('SELECT id,time,body,read,delivery FROM alerts WHERE (? IS NULL OR time<?) ORDER BY time DESC LIMIT ?', (before,before,min(limit,300))).fetchall()
        return [dict(json.loads(body),id=i,time=t,read=bool(r),delivery=d) for i,t,body,r,d in rows]
    def emit(self,key,title,message,kind='info',symbol=None,evidence=None):
        data={'title':title,'message':message,'kind':kind,'symbol':symbol,'evidence':evidence or {}}
        with self.store.lock,self.store.db:
            added=self.store.db.execute('INSERT OR IGNORE INTO alerts (id,time,body,delivery) VALUES (?,?,?,?)',
                 (key,now().isoformat(),json.dumps(data,ensure_ascii=False),'queued' if self.settings['enabled'] and self.settings['desktop'] else 'page_only')).rowcount
        if added and self.settings['enabled'] and self.settings['desktop']:self.queue.put_nowait((key,data))
        return bool(added)
    def read(self,key):
        with self.store.lock,self.store.db:self.store.db.execute('UPDATE alerts SET read=1 WHERE id=?',(key,))
    def configure(self,settings):
        self.settings.update(settings);self.store.set('notification_settings',self.settings)
    def status(self):return {**self.settings,'permission':self.permission,'last_error':self.last_error,'ai_calls':0}
    async def native(self,mode,data=None):
        path=self.root/'.local/ShortlistNotifier.app/Contents/MacOS/ShortlistNotifier'
        if sys.platform!='darwin' or not path.exists():return {'status':'unavailable'}
        p=await asyncio.create_subprocess_exec(str(path),mode,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
        try:
            out,_=await asyncio.wait_for(p.communicate(json.dumps(data or {},ensure_ascii=False).encode()),12)
            return json.loads(out)
        except (asyncio.TimeoutError,ValueError):
            if p.returncode is None:p.kill();await p.wait()
            return {'status':'error'}
    async def worker(self):
        for attempt in range(3):
            self.permission=(await self.native('status')).get('status','unknown')
            if self.permission not in ['error','unknown']:break
            await asyncio.sleep(attempt+1)
        # Pending messages from an earlier process are history, never replayed as new buys.
        with self.store.lock,self.store.db:self.store.db.execute("UPDATE alerts SET delivery='not_replayed' WHERE delivery='queued'")
        while True:
            try:key,data=await asyncio.wait_for(self.queue.get(),60)
            except asyncio.TimeoutError:
                status=(await self.native('status')).get('status','unknown')
                if status not in ['error','unknown']:self.permission=status
                continue
            if not self.settings['enabled'] or not self.settings['desktop']:result={'status':'page_only'}
            else:result=await self.native('send',{**data,'id':key,'sound':self.settings['sound']})
            state=result.get('status','error')
            if state in ['denied','not_determined','authorized','provisional']:self.permission=state
            elif state=='submitted':self.permission='authorized'
            self.last_error=None if state in ['submitted','page_only'] else state
            with self.store.lock,self.store.db:self.store.db.execute('UPDATE alerts SET delivery=? WHERE id=?',(state,key))
            self.queue.task_done()
