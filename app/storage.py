"""SQLite transactions; never stores Xray secrets or Telegram Bot Token."""
from __future__ import annotations
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path


def timestamp() -> int:
    return int(time.time())


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=20)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA busy_timeout=20000')
        self._init()

    def _init(self):
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS users (
          tg_id INTEGER PRIMARY KEY, username TEXT NOT NULL DEFAULT '', first_name TEXT NOT NULL DEFAULT '',
          created_at INTEGER NOT NULL, seen_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bindings (
          tg_id INTEGER PRIMARY KEY REFERENCES users(tg_id),
          email TEXT NOT NULL UNIQUE, manual_block INTEGER NOT NULL DEFAULT 0,
          created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS requests (
          id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER NOT NULL REFERENCES users(tg_id),
          kind TEXT NOT NULL CHECK(kind IN ('create','renew','hwid_delete','hwid_slots','adjust','block','unblock','delete')),
          requested_days INTEGER, device_id INTEGER, requested_slots INTEGER,
          status TEXT NOT NULL DEFAULT 'pending',
          settings TEXT NOT NULL DEFAULT '{}', result TEXT NOT NULL DEFAULT '',
          created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
          reviewer INTEGER
        );
        CREATE UNIQUE INDEX IF NOT EXISTS pending_request_per_user_kind
          ON requests(tg_id,kind) WHERE status IN ('pending','processing','needs_review');
        CREATE TABLE IF NOT EXISTS operations (
          id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER NOT NULL UNIQUE REFERENCES requests(id),
          kind TEXT NOT NULL, email TEXT NOT NULL, stage TEXT NOT NULL,
          plan TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ui_messages (
          chat_id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL, updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit (
          id INTEGER PRIMARY KEY AUTOINCREMENT, tg_admin INTEGER NOT NULL, tg_user INTEGER,
          event TEXT NOT NULL, details TEXT NOT NULL, created_at INTEGER NOT NULL
        );
        ''')
        self._migrate_requests_for_delete()
        self._migrate_display_names()
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS tickets (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tg_id INTEGER NOT NULL REFERENCES users(tg_id),
          status TEXT NOT NULL CHECK(status IN ('open','closed')) DEFAULT 'open',
          created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
          closed_at INTEGER, closed_by INTEGER
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_open_ticket_per_user
          ON tickets(tg_id) WHERE status='open';
        CREATE TABLE IF NOT EXISTS ticket_messages (
          ticket_id INTEGER NOT NULL REFERENCES tickets(id),
          chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
          deleted INTEGER NOT NULL DEFAULT 0,
          created_at INTEGER NOT NULL,
          PRIMARY KEY(chat_id,message_id)
        );
        ''')
        # Backward-compatible schema migration: keep the last progress marker.
        names = {r[1] for r in self.db.execute('PRAGMA table_info(operations)')}
        if 'last_progress_stage' not in names:
            self.db.execute("ALTER TABLE operations ADD COLUMN last_progress_stage TEXT NOT NULL DEFAULT ''")
        # Fresh bot databases must not reuse small request IDs from old Telegram
        # inline keyboards after a clean reinstall. Keep IDs on existing DBs intact.
        has_requests = self.db.execute('SELECT 1 FROM requests LIMIT 1').fetchone() is not None
        sequence = self.db.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='requests'"
        ).fetchone()
        if not has_requests and sequence is None:
            self.db.execute(
                "INSERT INTO sqlite_sequence(name, seq) VALUES('requests', ?)",
                (10_000_000 + secrets.randbelow(89_000_000),),
            )

    def _migrate_display_names(self):
        """Display aliases are independent of ASCII 3x-ui client identifiers.

        Existing approved create requests are the source of truth when upgrading
        an earlier schema, where a Cyrillic alias was recorded in request.settings but lost
        from the bindings listing. No credentials or remote records are changed.
        """
        columns={r[1] for r in self.db.execute('PRAGMA table_info(bindings)')}
        if 'display_name' not in columns:
            self.db.execute("ALTER TABLE bindings ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")
        rows=self.db.execute("""SELECT b.tg_id, r.settings FROM bindings b
            JOIN requests r ON r.tg_id=b.tg_id
            WHERE b.display_name='' AND r.kind='create' AND r.status='approved'
            ORDER BY r.id DESC""").fetchall()
        updated=set()
        from app.security import validate_alias
        for row in rows:
            uid=row['tg_id']
            if uid in updated: continue
            try:
                settings=json.loads(row['settings'] or '{}')
                if not isinstance(settings,dict): continue
                alias=settings.get('alias')
                if not alias: continue
                clean=validate_alias(alias)
            except (ValueError,TypeError,KeyError,json.JSONDecodeError):
                continue
            self.db.execute("UPDATE bindings SET display_name=? WHERE tg_id=? AND display_name=''",(clean,uid))
            updated.add(uid)

    def set_display_name(self,tg_id:int,alias:str):
        from app.security import validate_alias
        clean=validate_alias(alias)
        cur=self.db.execute('UPDATE bindings SET display_name=? WHERE tg_id=?',(clean,tg_id))
        if cur.rowcount != 1:
            raise ValueError('Telegram ID не привязан к клиенту')
        return clean

    def _migrate_requests_for_delete(self):
        # SQLite does not permit ALTERing a CHECK constraint. Preserve all IDs,
        # dependent operation records, pending indices and AUTOINCREMENT sequence.
        row=self.db.execute("SELECT sql FROM sqlite_master WHERE name='requests' AND type='table'").fetchone()
        if not row or "'delete'" in row[0]:
            return
        previous=self.db.execute("SELECT seq FROM sqlite_sequence WHERE name='requests'").fetchone()
        max_seq=int(previous[0]) if previous else 0
        self.db.execute('PRAGMA foreign_keys=OFF')
        try:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute('''CREATE TABLE requests_new (
              id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER NOT NULL REFERENCES users(tg_id),
              kind TEXT NOT NULL CHECK(kind IN ('create','renew','hwid_delete','hwid_slots','adjust','block','unblock','delete')),
              requested_days INTEGER, device_id INTEGER, requested_slots INTEGER,
              status TEXT NOT NULL DEFAULT 'pending', settings TEXT NOT NULL DEFAULT '{}',
              result TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL, reviewer INTEGER)''')
            self.db.execute('''INSERT INTO requests_new SELECT id,tg_id,kind,requested_days,device_id,requested_slots,
                status,settings,result,created_at,updated_at,reviewer FROM requests''')
            self.db.execute('DROP TABLE requests')
            self.db.execute('ALTER TABLE requests_new RENAME TO requests')
            self.db.execute('''CREATE UNIQUE INDEX pending_request_per_user_kind ON requests(tg_id,kind)
                WHERE status IN ('pending','processing','needs_review')''')
            self.db.execute("UPDATE sqlite_sequence SET seq=? WHERE name='requests'",(max_seq,))
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK')
            raise
        finally:
            self.db.execute('PRAGMA foreign_keys=ON')
        faults=self.db.execute('PRAGMA foreign_key_check').fetchall()
        if faults:
            raise RuntimeError('Нарушение ссылок SQLite после миграции requests')

    def unbind(self,tg_id:int):
        self.db.execute('DELETE FROM bindings WHERE tg_id=?',(tg_id,))

    def pending_others(self,tg_id:int,exclude:int):
        return self.db.execute('''SELECT COUNT(*) FROM requests
              WHERE tg_id=? AND id != ? AND status IN ('pending','processing','needs_review')''',(tg_id,exclude)).fetchone()[0]

    def open_ticket(self,tg_id:int):
        return self._row(self.db.execute("SELECT * FROM tickets WHERE tg_id=? AND status='open'",(tg_id,)).fetchone())

    def create_ticket(self,tg_id:int):
        if not self.user(tg_id): self.upsert_user(tg_id)
        old=self.open_ticket(tg_id)
        if old:return old['id']
        cur=self.db.execute("INSERT INTO tickets(tg_id,created_at,updated_at) VALUES(?,?,?)",(tg_id,timestamp(),timestamp()))
        return cur.lastrowid

    def ticket(self,ticket_id:int):
        return self._row(self.db.execute('SELECT * FROM tickets WHERE id=?',(ticket_id,)).fetchone())

    def tickets_open(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM tickets WHERE status='open' ORDER BY updated_at DESC LIMIT 40")]

    def track_ticket(self,ticket_id:int,chat_id:int,message_id:int):
        self.db.execute("""INSERT INTO ticket_messages(ticket_id,chat_id,message_id,created_at)
               VALUES(?,?,?,?) ON CONFLICT(chat_id,message_id) DO UPDATE SET
               ticket_id=excluded.ticket_id,deleted=0,created_at=excluded.created_at""",
               (ticket_id,chat_id,message_id,timestamp()))
        self.db.execute("UPDATE tickets SET updated_at=? WHERE id=?",(timestamp(),ticket_id))

    def recent_ticket_messages(self,ticket_id:int,chat_id:int,seconds:int=60):
        cutoff=timestamp()-seconds
        return self.db.execute('''SELECT COUNT(*) FROM ticket_messages
            WHERE ticket_id=? AND chat_id=? AND created_at>=?''',
            (ticket_id,chat_id,cutoff)).fetchone()[0]

    def close_ticket(self,ticket_id:int,closed_by:int):
        cur=self.db.execute("UPDATE tickets SET status='closed',updated_at=?,closed_at=?,closed_by=? WHERE id=? AND status='open'",
                            (timestamp(),timestamp(),closed_by,ticket_id))
        return cur.rowcount==1

    def ticket_messages(self,ticket_id:int):
        return [dict(row) for row in self.db.execute("SELECT chat_id,message_id,created_at FROM ticket_messages WHERE ticket_id=? AND deleted=0 ORDER BY created_at DESC",(ticket_id,))]

    def ticket_deleted(self,ticket_id:int,chat_id:int,message_id:int):
        self.db.execute("UPDATE ticket_messages SET deleted=1 WHERE ticket_id=? AND chat_id=? AND message_id=?",(ticket_id,chat_id,message_id))

    def pending_ticket_cleanup(self,ticket_id:int):
        return self.db.execute("SELECT COUNT(*) FROM ticket_messages WHERE ticket_id=? AND deleted=0",(ticket_id,)).fetchone()[0]

    def _row(self, row):
        return dict(row) if row else None

    def user(self, tg_id: int):
        return self._row(self.db.execute('SELECT * FROM users WHERE tg_id=?',(tg_id,)).fetchone())

    def upsert_user(self, tg_id:int, username:str='', first_name:str=''):
        now=timestamp()
        self.db.execute('''INSERT INTO users(tg_id,username,first_name,created_at,seen_at)
          VALUES (?,?,?,?,?) ON CONFLICT(tg_id) DO UPDATE SET
          username=CASE WHEN excluded.username <> '' THEN excluded.username ELSE users.username END,
          first_name=CASE WHEN excluded.first_name <> '' THEN excluded.first_name ELSE users.first_name END,
          seen_at=excluded.seen_at''',
          (tg_id,username or '',first_name or '',now,now))

    def bind(self, tg_id:int, email:str, display_name:str=''):
        if not self.user(tg_id):
            self.upsert_user(tg_id)
        from app.security import validate_alias
        clean=validate_alias(display_name) if display_name else ''
        self.db.execute('INSERT INTO bindings(tg_id,email,created_at,display_name) VALUES (?,?,?,?)',
                        (tg_id,email,timestamp(),clean))

    def binding(self,tg_id:int):
        return self._row(self.db.execute('SELECT * FROM bindings WHERE tg_id=?',(tg_id,)).fetchone())

    def binding_email(self,email:str):
        return self._row(self.db.execute('SELECT * FROM bindings WHERE email=?',(email,)).fetchone())

    def bindings(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM bindings ORDER BY created_at DESC')]

    def set_block(self,tg_id:int, blocked:bool):
        self.db.execute('UPDATE bindings SET manual_block=? WHERE tg_id=?',(int(blocked),tg_id))

    def create_request(self,tg_id:int,kind:str,days=None,device_id=None,slots=None)->int:
        if kind not in ('create','renew','hwid_delete','hwid_slots','adjust','block','unblock','delete'):
            raise ValueError('Неизвестный тип заявки')
        try:
            cur=self.db.execute('''INSERT INTO requests(tg_id,kind,requested_days,device_id,requested_slots,created_at,updated_at)
                  VALUES (?,?,?,?,?,?,?)''',(tg_id,kind,days,device_id,slots,timestamp(),timestamp()))
            return cur.lastrowid
        except sqlite3.IntegrityError as e:
            raise ValueError('У вас уже есть открытая заявка этого типа') from e

    def request(self,request_id:int):
        return self._row(self.db.execute('SELECT * FROM requests WHERE id=?',(request_id,)).fetchone())

    def pending(self,limit:int=30):
        return [dict(r) for r in self.db.execute("SELECT * FROM requests WHERE status IN ('pending','needs_review','processing') ORDER BY id DESC LIMIT ?",(limit,))]

    def requests_for(self,tg_id:int):
        return [dict(r) for r in self.db.execute('SELECT * FROM requests WHERE tg_id=? ORDER BY id DESC LIMIT 10',(tg_id,))]

    def settings(self,req:dict)->dict:
        return json.loads(req['settings'] or '{}')

    def set_settings(self,request_id:int, **changes):
        row=self.request(request_id)
        if not row or row['status']!='pending':
            raise ValueError('Заявка больше не редактируется')
        cfg=self.settings(row)
        cfg.update(changes)
        self.db.execute('UPDATE requests SET settings=?,updated_at=? WHERE id=?',
                        (json.dumps(cfg,ensure_ascii=False),timestamp(),request_id))

    def remove_settings(self,request_id:int,*fields:str):
        row=self.request(request_id)
        if not row or row['status']!='pending':raise ValueError('Заявка больше не редактируется')
        cfg=self.settings(row)
        for field in fields:cfg.pop(field,None)
        self.db.execute('UPDATE requests SET settings=?,updated_at=? WHERE id=?',
                        (json.dumps(cfg,ensure_ascii=False),timestamp(),request_id))

    def transition(self, request_id:int,expected:str,next_state:str,result:str='',reviewer:int|None=None)->bool:
        cur=self.db.execute('''UPDATE requests SET status=?,result=?,reviewer=COALESCE(?,reviewer),updated_at=?
          WHERE id=? AND status=?''',(next_state,result,reviewer,timestamp(),request_id,expected))
        return cur.rowcount==1

    def add_operation(self,request_id:int,kind:str,email:str,plan:dict):
        self.db.execute('''INSERT INTO operations(request_id,kind,email,stage,plan,created_at,updated_at)
          VALUES (?,?,?,?,?,?,?)''',(request_id,kind,email,'planned',json.dumps(plan,ensure_ascii=False),timestamp(),timestamp()))

    def operation(self,request_id:int):
        row=self._row(self.db.execute('SELECT * FROM operations WHERE request_id=?',(request_id,)).fetchone())
        if row: row['plan']=json.loads(row['plan'])
        return row

    def operation_stage(self,request_id:int,stage:str):
        if stage == 'needs_review':
            self.db.execute(
                "UPDATE operations SET last_progress_stage=CASE "
                "WHEN stage NOT IN ('needs_review','complete') THEN stage "
                "ELSE last_progress_stage END, stage=?, updated_at=? WHERE request_id=?",
                (stage,timestamp(),request_id))
        else:
            self.db.execute('UPDATE operations SET stage=?,updated_at=? WHERE request_id=?',
                            (stage,timestamp(),request_id))

    def unfinished(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM operations WHERE stage!='complete' ORDER BY id DESC")]

    def audit(self,admin:int,user:int|None,event:str,details:str):
        self.db.execute('INSERT INTO audit(tg_admin,tg_user,event,details,created_at) VALUES (?,?,?,?,?)',
                        (admin,user,event,details[:500],timestamp()))

    def audit_list(self,limit:int=20):
        return [dict(r) for r in self.db.execute('SELECT * FROM audit ORDER BY id DESC LIMIT ?',(limit,))]

    def resolve_manual(self,request_id:int,admin:int,outcome:str):
        if outcome not in ('approved','rejected'):
            raise ValueError('Разрешено: approved или rejected')
        req=self.request(request_id)
        if not req or req['status']!='needs_review':
            raise ValueError('Нужна заявка в статусе needs_review')
        target='approved' if outcome=='approved' else 'rejected'
        self.transition(request_id,'needs_review',target,'Результат вручную сверен администратором в 3x-ui',admin)
        self.audit(admin,req['tg_id'],'manual_resolution',f'Заявка #{request_id}: {outcome}')
        return req

    def reconcile_stuck(self):
        # Called on startup: unknown external outcome must be manually reviewed, not retried.
        self.db.execute("UPDATE requests SET status='needs_review', result='Сбой или перезапуск во время операции: проверьте 3x-ui',updated_at=? WHERE status='processing'",(timestamp(),))
        self.db.execute(
            "UPDATE operations SET last_progress_stage=stage, stage='needs_review', updated_at=? "
            "WHERE stage NOT IN ('complete','complete_manual','needs_review')",
            (timestamp(),))

    def ui_screen(self, chat_id: int) -> int | None:
        row=self.db.execute('SELECT message_id FROM ui_messages WHERE chat_id=?',(chat_id,)).fetchone()
        return int(row['message_id']) if row else None

    def ui_set(self, chat_id: int, message_id: int):
        self.db.execute('''INSERT INTO ui_messages(chat_id,message_id,updated_at)
           VALUES(?,?,?) ON CONFLICT(chat_id) DO UPDATE SET
           message_id=excluded.message_id,updated_at=excluded.updated_at''',
           (chat_id,message_id,timestamp()))

    def close(self):
        self.db.close()
