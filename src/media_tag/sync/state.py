"""Bridge-only durable inbox/cursor/outbox; never writes the media catalogue."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3

from media_contracts import IntakeEvent, canonical, digest
from media_tag.storage.filesystem import require_local_path
from .client import SyncError


class State:
    def __init__(self,root,scope):
        self.root=Path(root)
        if not self.root.is_absolute() or str(root).replace('\\','/').startswith('//'):raise SyncError('local_sync_state_required')
        require_local_path(self.root);self.root.mkdir(parents=True,exist_ok=True)
        (self.root/'README.md').write_text('# Private sync state\n\nDurable event inbox, source downloads and publication outbox. No credentials; not Git.\n',encoding='utf-8')
        self.downloads=self.root/'downloads';self.downloads.mkdir(exist_ok=True)
        (self.downloads/'README.md').write_text('Private verified cloud intake bytes, never overwrite original sources.\n',encoding='utf-8')
        self.bundles=self.root/'bundles';self.bundles.mkdir(exist_ok=True)
        (self.bundles/'README.md').write_text('Immutable generated publication attempts, retained on failure for diagnosis.\n',encoding='utf-8')
        self.path=self.root/'sync.db'
        with self.db(True) as c:
            c.execute('CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            old=c.execute("SELECT value FROM metadata WHERE key='scope'").fetchone()
            if old and old[0]!=digest(scope):raise SyncError('sync_scope_changed')
            c.execute("INSERT OR IGNORE INTO metadata VALUES('scope',?)",(digest(scope),))
            c.execute("INSERT OR IGNORE INTO metadata VALUES('cursor','0')")
            c.execute('CREATE TABLE IF NOT EXISTS owners(cloud_actor TEXT PRIMARY KEY,binding TEXT NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS events(sequence INTEGER PRIMARY KEY,event_id TEXT UNIQUE NOT NULL,payload TEXT NOT NULL,local_upload TEXT,asset_id TEXT,job_id TEXT,classification TEXT,phase INTEGER NOT NULL DEFAULT 0,tag_proof TEXT NOT NULL DEFAULT \'[]\',batch_id TEXT,last_error TEXT)')
            c.execute('CREATE TABLE IF NOT EXISTS batches(batch_id TEXT PRIMARY KEY,payload TEXT NOT NULL,status TEXT NOT NULL)')

    @contextmanager
    def db(self,write=False):
        c=sqlite3.connect(self.path,isolation_level=None,timeout=10);c.row_factory=sqlite3.Row
        try:
            c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA synchronous=FULL');c.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield c;c.execute('COMMIT')
        except BaseException:
            if c.in_transaction:c.execute('ROLLBACK')
            raise
        finally:c.close()

    @contextmanager
    def lock(self):
        import os
        handle=(self.root/'run.lock').open('a+b');locked=False
        try:
            if os.name=='nt':
                import msvcrt
                handle.seek(0,2)
                if handle.tell()==0:handle.write(b'0');handle.flush()
                handle.seek(0)
                try:msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
                except OSError:raise SyncError('sync_already_running') from None
            else:
                import fcntl
                try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except OSError:raise SyncError('sync_already_running') from None
            locked=True;yield
        finally:
            if locked:
                if os.name=='nt':handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
                else:fcntl.flock(handle,fcntl.LOCK_UN)
            handle.close()

    def cursor(self):
        with self.db() as c:return int(c.execute("SELECT value FROM metadata WHERE key='cursor'").fetchone()[0])

    def receive_page(self,page):
        after=self.cursor();items=[IntakeEvent.model_validate(e) for e in page['items']]
        expected=items[-1].sequence if items else after
        if type(page['next_cursor']) is not int or page['next_cursor']!=expected or any(e.feed_id!=page['feed_id'] for e in items):raise SyncError('invalid_intake_page')
        numbers=[e.sequence for e in items]
        if numbers!=sorted(set(numbers)) or numbers and numbers[0]<=after:raise SyncError('invalid_intake_sequence')
        with self.db(True) as c:
            feed=c.execute("SELECT value FROM metadata WHERE key='feed_id'").fetchone()
            if feed and feed[0]!=page['feed_id']:raise SyncError('intake_feed_changed')
            c.execute("INSERT OR IGNORE INTO metadata VALUES('feed_id',?)",(page['feed_id'],))
            for event in items:
                raw=canonical(event).decode();old=c.execute('SELECT payload FROM events WHERE event_id=?',(event.event_id,)).fetchone()
                if old and old[0]!=raw:raise SyncError('intake_event_changed')
                collision=c.execute('SELECT event_id FROM events WHERE sequence=?',(event.sequence,)).fetchone()
                if collision and collision[0]!=event.event_id:raise SyncError('intake_sequence_changed')
                c.execute('INSERT OR IGNORE INTO events(sequence,event_id,payload) VALUES(?,?,?)',(event.sequence,event.event_id,raw))
            # Advance only after the entire page is durably queued; failed work stays in events.
            c.execute("UPDATE metadata SET value=? WHERE key='cursor'",(str(expected),))
        return len(items)

    def bind_owner(self,cloud_actor,local_actor,origin):
        binding=canonical([local_actor,origin]).decode()
        with self.db(True) as c:
            old=c.execute('SELECT binding FROM owners WHERE cloud_actor=?',(cloud_actor,)).fetchone()
            if old and old[0]!=binding:raise SyncError('intake_owner_binding_changed')
            c.execute('INSERT OR IGNORE INTO owners VALUES(?,?)',(cloud_actor,binding))

    def rows(self):
        with self.db() as c:return [dict(r) for r in c.execute('SELECT * FROM events WHERE phase<4 ORDER BY sequence')]

    def row(self,event_id):
        with self.db() as c:return dict(c.execute('SELECT * FROM events WHERE event_id=?',(event_id,)).fetchone())

    def update(self,event_id,**values):
        if set(values)-{'local_upload','asset_id','job_id','classification','phase','tag_proof','batch_id','last_error'}:raise ValueError('invalid sync state field')
        with self.db(True) as c:c.execute('UPDATE events SET '+','.join(k+'=?' for k in values)+' WHERE event_id=?',(*values.values(),event_id))

    def summary(self):
        with self.db() as c:
            counts={r[0]:r[1] for r in c.execute('SELECT phase,count(*) FROM events GROUP BY phase')}
            return {'cursor':self.cursor(),'total':sum(counts.values()),'cloud_active':counts.get(4,0),'pending':sum(v for k,v in counts.items() if k<4),
                    'errors':[{'event_id':r[0],'code':r[1]} for r in c.execute('SELECT event_id,last_error FROM events WHERE last_error IS NOT NULL')]}
