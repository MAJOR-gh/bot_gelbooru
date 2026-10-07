"""Disk-backed temporary full-search index; no unbounded Python post list.

The SQLite file is unlinked immediately after opening (Linux/macOS). It is a
cache, NOT history, and disappears when the process closes its file descriptor.
On Windows it is removed on close. Candidate iteration is lazy and ranked in SQL.
"""
import contextlib
import json
import os
import sqlite3
import tempfile
import uuid
from collections.abc import Sequence


class SearchIndex:
    def __init__(self):
        directory = os.environ.get('DATA_DIR') or os.path.dirname(os.path.abspath(__file__))
        os.makedirs(directory, exist_ok=True)
        fd, self.path = tempfile.mkstemp(prefix='.search-index-', suffix='.sqlite3', dir=directory)
        os.close(fd)
        self.db = sqlite3.connect(self.path)
        self.db.execute('PRAGMA journal_mode=OFF')
        self.db.execute('PRAGMA synchronous=OFF')  # disposable cache, not delivery history
        self.db.execute('PRAGMA cache_size=-4096')  # ~4 MiB per cached query
        self.db.executescript('''
            CREATE TABLE posts (
                uid TEXT PRIMARY KEY, site_id TEXT NOT NULL, payload TEXT NOT NULL,
                allowed INTEGER NOT NULL, strict INTEGER NOT NULL,
                tier INTEGER NOT NULL, score INTEGER NOT NULL, ordinal INTEGER NOT NULL
            );
            CREATE INDEX ranked ON posts(allowed, strict DESC, tier DESC, score DESC, ordinal);
            CREATE TABLE excluded (request TEXT NOT NULL, uid TEXT NOT NULL,
                                   PRIMARY KEY(request,uid));
        ''')
        self.db.commit()
        if os.name != 'nt':
            os.unlink(self.path)

    def add(self, records) -> int:
        allowed = 0
        with self.db:
            for record in records:
                cursor = self.db.execute('INSERT OR IGNORE INTO posts VALUES (?,?,?,?,?,?,?,?)', record)
                if cursor.rowcount:
                    allowed += record[3]
        return allowed

    def candidates(self, seen) -> 'Candidates':
        return Candidates(self, seen)

    def close(self):
        db = getattr(self, 'db', None)
        if db is not None:
            db.close()
            self.db = None
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
