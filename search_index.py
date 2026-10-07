"""Disk-backed incremental index, persistent cursor, lazy ranked candidates.

DATA_DIR/search_cache is disposable API metadata, distinct from shown.sqlite3.
Requests keep independent TEMP exclusion snapshots; no downloaded media here.
"""
import contextlib
import json
import hashlib
import os
import sqlite3
import tempfile
import uuid
from collections.abc import Sequence


class SearchIndex:
    def __init__(self, cache_key=None):
        directory = os.environ.get('DATA_DIR') or os.path.dirname(os.path.abspath(__file__))
        os.makedirs(directory, exist_ok=True)
        self.persistent = cache_key is not None
        if self.persistent:
            directory = os.path.join(directory, 'search_cache')
            os.makedirs(directory, exist_ok=True)
            digest = hashlib.sha256(json.dumps(cache_key, sort_keys=True).encode()).hexdigest()
            self.path = os.path.join(directory, digest + '.sqlite3')
        else:
            fd, self.path = tempfile.mkstemp(prefix='.search-index-', suffix='.sqlite3', dir=directory)
            os.close(fd)
        self.db = sqlite3.connect(self.path)
        self.db.execute('PRAGMA journal_mode=WAL' if self.persistent else 'PRAGMA journal_mode=OFF')
        self.db.execute('PRAGMA synchronous=NORMAL' if self.persistent else 'PRAGMA synchronous=OFF')
        self.db.execute('PRAGMA cache_size=-4096')  # ~4 MiB per cached query
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS posts (
                uid TEXT PRIMARY KEY, site_id TEXT NOT NULL, payload TEXT NOT NULL,
                allowed INTEGER NOT NULL, strict INTEGER NOT NULL,
                tier INTEGER NOT NULL, score INTEGER NOT NULL, ordinal INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ranked ON posts(allowed, strict DESC, tier DESC, score DESC, ordinal);
            CREATE TABLE IF NOT EXISTS progress (key TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pages (page INTEGER PRIMARY KEY, signature TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS page_signature ON pages(signature);
            CREATE TEMP TABLE excluded (request TEXT NOT NULL, uid TEXT NOT NULL,
                                   PRIMARY KEY(request,uid));
        ''')
        self.db.commit()
        if not self.persistent and os.name != 'nt':
            os.unlink(self.path)

    def add(self, records) -> int:
        allowed = 0
        with self.db:
            for record in records:
                cursor = self.db.execute('INSERT OR IGNORE INTO posts VALUES (?,?,?,?,?,?,?,?)', record)
                if cursor.rowcount:
                    allowed += record[3]
        return allowed

    def progress(self):
        row = self.db.execute("SELECT payload FROM progress WHERE key='state'").fetchone()
        return json.loads(row[0]) if row else {}

    def save_progress(self, state, page=None, signature=None):
        with self.db:
            if page is not None:
                self.db.execute('INSERT OR REPLACE INTO pages VALUES (?,?)', (page, signature))
            self.db.execute("INSERT OR REPLACE INTO progress VALUES ('state',?)", (json.dumps(state),))

    def repeated_page(self, page, signature):
        return self.db.execute('SELECT 1 FROM pages WHERE signature=? AND page!=? LIMIT 1',
                               (signature, page)).fetchone() is not None

    def reset_progress(self):
        # Keep indexed candidates: refreshing must not block useful existing results.
        with self.db:
            self.db.execute('DELETE FROM progress')
            self.db.execute('DELETE FROM pages')

    def allowed_count(self):
        return self.db.execute('SELECT COUNT(*) FROM posts WHERE allowed=1').fetchone()[0]

    def candidates(self, seen) -> 'Candidates':
        return Candidates(self, seen)

    def close(self):
        db = getattr(self, 'db', None)
        if db is not None:
            db.close()
            self.db = None
        if not getattr(self, 'persistent', False):
            with contextlib.suppress(FileNotFoundError, PermissionError):
                os.unlink(self.path)

    def __del__(self):
        with contextlib.suppress(Exception):
            self.close()


class Candidates(Sequence):
    """Read-only lazy sequence; each request has its own excluded-ID snapshot."""
    def __init__(self, index: SearchIndex, seen):
        self.index = index  # keeps cache alive during downloads, even after LRU eviction
        self.request = uuid.uuid4().hex
        with index.db:
            index.db.executemany('INSERT INTO excluded VALUES (?,?)',
                                 [(self.request, uid) for uid in set(seen)])

    def _sql(self, selection='p.payload', ordered=True):
        query = f'''SELECT {selection} FROM posts p WHERE p.allowed=1 AND NOT EXISTS (
            SELECT 1 FROM excluded e WHERE e.request=? AND e.uid IN (p.uid,p.site_id)
        )'''
        if ordered:
            query += ' ORDER BY p.strict DESC,p.tier DESC,p.score DESC,p.ordinal'
        return query

    def __bool__(self):
        return self.index.db.execute(self._sql('1', False) + ' LIMIT 1',
                                     (self.request,)).fetchone() is not None

    def __len__(self):
        return self.index.db.execute(self._sql('COUNT(*)', False),
                                     (self.request,)).fetchone()[0]

    def __iter__(self):
        cursor = self.index.db.execute(self._sql(), (self.request,))
        try:
            for row in cursor:
                yield json.loads(row[0])
        finally:
            cursor.close()

    def __getitem__(self, item):
        if isinstance(item, slice):
            return [self[i] for i in range(*item.indices(len(self)))]
        if item < 0:
            item += len(self)
        if item < 0:
            raise IndexError(item)
        row = self.index.db.execute(self._sql() + ' LIMIT 1 OFFSET ?',
                                    (self.request, item)).fetchone()
        if row is None:
            raise IndexError(item)
        return json.loads(row[0])

    def __eq__(self, other):
        if isinstance(other, list) and not other:
            return not bool(self)
        return list(self) == other

    def __del__(self):
        with contextlib.suppress(Exception):
            with self.index.db:
                self.index.db.execute('DELETE FROM excluded WHERE request=?', (self.request,))
