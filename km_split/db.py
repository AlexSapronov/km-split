import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path):
        self.path = path
        with self.connect() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sources (
              cis TEXT PRIMARY KEY, full TEXT NOT NULL, gtin TEXT NOT NULL,
              inn TEXT NOT NULL, pn TEXT NOT NULL, baseline INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS operations (
              id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL,
              source TEXT NOT NULL REFERENCES sources(cis), inn TEXT NOT NULL,
              quantity INTEGER NOT NULL, pn TEXT NOT NULL, state TEXT NOT NULL,
              order_id TEXT, new_code TEXT, block_id TEXT, report_id TEXT,
              error TEXT, created TEXT NOT NULL, updated TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, kind TEXT NOT NULL, inn TEXT NOT NULL,
              target TEXT, body TEXT NOT NULL, state TEXT NOT NULL,
              remote_id TEXT, error TEXT, created TEXT NOT NULL, updated TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS report_items (
              job_id TEXT NOT NULL REFERENCES jobs(id),
              operation_id TEXT NOT NULL REFERENCES operations(id),
              PRIMARY KEY(job_id,operation_id));
            CREATE TABLE IF NOT EXISTS tokens (
              inn TEXT NOT NULL, kind TEXT NOT NULL, token TEXT NOT NULL,
              updated TEXT NOT NULL, PRIMARY KEY(inn,kind));
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY, at TEXT NOT NULL, entity TEXT NOT NULL,
              action TEXT NOT NULL, detail TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS auto_split_batches (
              id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL,
              source_cis TEXT NOT NULL, source_code TEXT NOT NULL, parts TEXT NOT NULL,
              operation_ids TEXT NOT NULL DEFAULT '[]', expected_codes TEXT NOT NULL DEFAULT '[]',
              report_job_ids TEXT NOT NULL DEFAULT '[]', state TEXT NOT NULL,
              error TEXT, created TEXT NOT NULL, updated TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS signer_leases (
              job_id TEXT PRIMARY KEY REFERENCES jobs(id), token TEXT NOT NULL,
              sha256 TEXT NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS signer_heartbeats (
              id INTEGER PRIMARY KEY CHECK(id=1), thumbprint TEXT NOT NULL,
              expires TEXT NOT NULL, status TEXT NOT NULL, time TEXT NOT NULL);
            ''')
        path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def one(self, sql, params=()):
        with self.connect() as db:
            row = db.execute(sql, params).fetchone()
            return dict(row) if row else None

    def all(self, sql, params=()):
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, params)]

    @staticmethod
    def event(db, entity, action, detail=''):
        db.execute('INSERT INTO events(at,entity,action,detail) VALUES(?,?,?,?)', (now(), entity, action, detail))
