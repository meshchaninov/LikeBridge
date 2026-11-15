import contextlib
import json
import os
import sqlite3
from pathlib import Path
from cryptography.fernet import Fernet


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        key = self.directory / 'encryption.key'
        try:
            fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'wb') as f:
                f.write(Fernet.generate_key())
        self.cipher = Fernet(key.read_bytes())
        self.path = str(self.directory / 'likebridge.db')
        with self.db() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS accounts (
                    provider TEXT PRIMARY KEY, credentials TEXT NOT NULL, enabled INTEGER DEFAULT 1,
                    initialized INTEGER DEFAULT 0, last_sync REAL, error TEXT, count INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS tracks (
                    id INTEGER PRIMARY KEY, provider TEXT NOT NULL, source_id TEXT NOT NULL,
                    title TEXT NOT NULL, artist TEXT NOT NULL, album TEXT NOT NULL,
                    year INTEGER, source_url TEXT, status TEXT DEFAULT 'pending',
                    mbid TEXT, candidates TEXT DEFAULT '[]', error TEXT,
                    attempts INTEGER DEFAULT 0, next_try REAL DEFAULT 0,
                    created REAL NOT NULL, updated REAL NOT NULL,
                    UNIQUE(provider, source_id));
                CREATE INDEX IF NOT EXISTS tracks_queue ON tracks(status, next_try);
                CREATE TABLE IF NOT EXISTS deliveries (
                    mbid TEXT PRIMARY KEY, status TEXT NOT NULL, response TEXT, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, at REAL NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS oauth (
                    state TEXT PRIMARY KEY, provider TEXT NOT NULL, data TEXT NOT NULL, expires REAL NOT NULL);
            ''')
            # Additive migration: preserve existing queue, statuses and delivery history.
            columns = {row['name'] for row in db.execute('PRAGMA table_info(tracks)')}
            if 'cover_url' not in columns:
                db.execute('ALTER TABLE tracks ADD COLUMN cover_url TEXT')
            if 'liked_at' not in columns:
                db.execute('ALTER TABLE tracks ADD COLUMN liked_at REAL')
            if 'request_kind' not in columns:
                db.execute("ALTER TABLE tracks ADD COLUMN request_kind TEXT NOT NULL DEFAULT 'album'")
            if 'track_artist' not in columns:
                db.execute('ALTER TABLE tracks ADD COLUMN track_artist TEXT')
            db.execute('CREATE INDEX IF NOT EXISTS tracks_liked_at ON tracks(liked_at DESC, id ASC)')
        os.chmod(self.path, 0o600)

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA cache_size=-1024')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def seal(self, value):
        return self.cipher.encrypt(json.dumps(value).encode()).decode()

    def unseal(self, value):
        return json.loads(self.cipher.decrypt(value.encode()))

    def get(self, key, default=None):
        with self.db() as db:
            row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return self.unseal(row['value']) if row else default

    def put(self, key, value):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, self.seal(value)))

    def credentials(self, provider):
        with self.db() as db:
            row = db.execute('SELECT credentials FROM accounts WHERE provider=?', (provider,)).fetchone()
        return self.unseal(row['credentials']) if row else {}

    def account(self, provider, credentials):
        with self.db() as db:
            # Keep the baseline on refresh, including a disconnect/reconnect.
            db.execute('''INSERT INTO accounts(provider, credentials) VALUES (?,?)
                ON CONFLICT(provider) DO UPDATE SET credentials=excluded.credentials, enabled=1, error=NULL''',
                (provider, self.seal(credentials)))

    def event(self, message, level='info'):
        import time
        with self.db() as db:
            db.execute('INSERT INTO events(at,level,message) VALUES (?,?,?)', (time.time(), level, message))
            db.execute('DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 200)')
