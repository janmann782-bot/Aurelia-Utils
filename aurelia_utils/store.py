"""SQLite WAL: persistent wizard, job queue and saved maps on a single VPS."""
import json
import shutil
import sqlite3
import time
import uuid
from pathlib import Path


class Busy(ValueError):
    pass


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "aurelia.sqlite3"
        with self.connect() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY, session TEXT NOT NULL DEFAULT '{}',
                animation INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY, user INTEGER NOT NULL, kind TEXT NOT NULL,
                params TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
                stage TEXT NOT NULL DEFAULT 'В очереди', done INTEGER DEFAULT 0,
                total INTEGER DEFAULT 0, error TEXT, created REAL NOT NULL,
                updated REAL NOT NULL, delivered INTEGER DEFAULT 0,
                chat INTEGER, message INTEGER);
            CREATE INDEX IF NOT EXISTS jobs_owner ON jobs(user,created DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_job ON jobs(user)
                WHERE status IN ('queued','running','cancelling');
            ''')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        return db

    def session(self, user):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO users(id) VALUES(?)", (user,))
            return json.loads(db.execute("SELECT session FROM users WHERE id=?", (user,)).fetchone()[0])

    def set_session(self, user, data):
        with self.connect() as db:
            db.execute("INSERT INTO users(id,session) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET session=excluded.session",
                       (user, json.dumps(data, ensure_ascii=False)))

    def animation(self, user, toggle=False):
        self.session(user)
        with self.connect() as db:
            if toggle:
                db.execute("UPDATE users SET animation=1-animation WHERE id=?", (user,))
            return bool(db.execute("SELECT animation FROM users WHERE id=?", (user,)).fetchone()[0])

    def enqueue(self, user, kind, params, chat=None, message=None, source=None):
        job_id = uuid.uuid4().hex[:16]
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running','cancelling')").fetchone()[0] >= 50:
                raise Busy("Очередь заполнена. Попробуй чуть позже")
            if db.execute("SELECT count(*) FROM jobs WHERE user=? AND status='ready'", (user,)).fetchone()[0] >= 50:
                raise Busy("Сохранено 50 работ. Удали ненужные через меню")
            try:
                db.execute("INSERT INTO jobs(id,user,kind,params,created,updated,chat,message) VALUES(?,?,?,?,?,?,?,?)",
                           (job_id, user, kind, json.dumps(params, ensure_ascii=False), now, now, chat, message))
            except sqlite3.IntegrityError as e:
                raise Busy("У тебя уже есть задача. Дождись результата или отмени ее") from e
            if source is not None:
                folder = self.root / "users" / str(user) / job_id
                folder.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, folder / 'source')
        return job_id

    def get(self, job_id, user=None):
        with self.connect() as db:
            if user is None:
                row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            else:
                row = db.execute("SELECT * FROM jobs WHERE id=? AND user=?", (job_id, user)).fetchone()
            return dict(row) if row else None

    def jobs(self, user, limit=8, offset=0):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM jobs WHERE user=? ORDER BY created DESC LIMIT ? OFFSET ?",
                                                (user, limit, offset))]

    def pending_delivery(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM jobs WHERE delivered=0 AND chat IS NOT NULL ORDER BY created LIMIT 100")]

    def delivered(self, job):
        with self.connect() as db:
            db.execute("UPDATE jobs SET delivered=1 WHERE id=?", (job,))

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET status='running',updated=? WHERE id=?", (time.time(), row['id']))
            return dict(row)

    def recover(self):
        # Called only after acquiring the exclusive worker lock.
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='cancelled',delivered=0 WHERE status='cancelling'")
            db.execute("UPDATE jobs SET status='queued',stage='Восстанавливаю после перезапуска',done=0,total=0 WHERE status='running'")

    def progress(self, job, stage, done=0, total=0):
        with self.connect() as db:
            row = db.execute("SELECT status FROM jobs WHERE id=?", (job,)).fetchone()
            if not row or row[0] in {'cancelled','cancelling'}:
                raise InterruptedError("Задача отменена")
            db.execute("UPDATE jobs SET stage=?,done=?,total=?,updated=? WHERE id=?",
                       (stage, done, total, time.time(), job))

    def finish(self, job, error=None):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status=?,error=?,updated=? WHERE id=? AND status='running'",
                       ('failed' if error else 'ready', error, time.time(), job))
            db.execute("UPDATE jobs SET status='cancelled' WHERE id=? AND status='cancelling'",(job,))

    def cancel(self, job, user):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status=CASE WHEN status IN ('running','cancelling') THEN 'cancelling' ELSE 'cancelled' END,stage='Отменяю задачу',updated=? WHERE id=? AND user=? AND status IN ('queued','running','cancelling')",
                       (time.time(), job, user))

    def acknowledge_cancel(self, job):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='cancelled' WHERE id=? AND status='cancelling'",(job,))

    def remove(self, job, user):
        with self.connect() as db:
            row = db.execute("SELECT status FROM jobs WHERE id=? AND user=?", (job, user)).fetchone()
            if row and row[0] not in {'running','queued','cancelling'}:
                db.execute("DELETE FROM jobs WHERE id=? AND user=?", (job, user))
                return True
        return False
