"""Строгий анти-повтор: ранжирование и долговечная история в SQLite.

Область истории — Discord-канал, а не запрос. Старые записи не вытесняются.
Резервирование перед отправкой атомарно; при неясном исходе отправки резерв
остаётся, чтобы повторный запрос не отправил потенциально доставленный файл.
"""
import json
import logging
import os
import sqlite3
import time
import uuid
from collections.abc import Iterable

from content_filter import focus_tier

logger = logging.getLogger('gelbooru_bot.selection')
LEGACY_SCOPE = '__legacy_all_channels__'


def post_score(post: dict) -> int:
    try:
        return int(post.get('score') or 0)
    except (TypeError, ValueError):
        return 0


def post_uid(post: dict) -> str:
    uid = post.get('_uid')
    if uid:
        return str(uid)
    md5 = str(post.get('md5') or '').lower()
    return md5 or f"{post.get('_site')}:{post.get('id')}"


def post_uids(post: dict) -> set[str]:
    """MD5 + id источника: смена метаданных не делает тот же пост новым."""
    ids = {post_uid(post)}
    if post.get('_legacy_uid'):
        ids.add(str(post['_legacy_uid']))
    if post.get('_site') and post.get('id') is not None:
        ids.add(f"{post['_site']}:{post['id']}")
    return ids


def channel_scope(interaction) -> str:
    channel_id = getattr(interaction, 'channel_id', None)
    if channel_id is None:
        channel_id = getattr(getattr(interaction, 'channel', None), 'id', None)
    if channel_id is None:
        raise ValueError('Нет Discord channel_id: отправка без области истории запрещена')
    return f"discord:{getattr(interaction, 'guild_id', None) or 'dm'}:{channel_id}"


def order_candidates(strict: list[dict], recent: Iterable[str],
                     groups: set[str] | None = None,
                     broad: list[dict] = ()) -> list[dict]:
    """Только непоказанные. Исчерпание выдачи НИКОГДА не разрешает повтор."""
    seen = set(recent)
    added: set[str] = set()

    def fresh(posts):
        result = []
        for post in sorted(posts, key=lambda p: (focus_tier(p, groups), post_score(p)),
                           reverse=True):
            ids = post_uids(post)
            if ids.isdisjoint(seen) and ids.isdisjoint(added):
                result.append(post)
                added.update(ids)
        return result

    return fresh(strict) + fresh(broad)


class ShownMemory:
    """Долговечный журнал sent/pending; SQLite — единственный источник истины.

    Короткие транзакции не пересекают await. WAL + synchronous=FULL сохраняют
    резерв ДО отправки. После crash pending не снимается автоматически: это
    намеренный fail-closed режим (лучше пропуск, чем дубль).
    """

    def __init__(self, path: str, legacy_path: str | None = None):
        self.path = path
        self.legacy_path = legacy_path
        self._db: sqlite3.Connection | None = None

    def load(self) -> None:
        self._connection()

    def _connection(self) -> sqlite3.Connection:
        if self._db is not None:
            return self._db
        if self.path != ':memory:':
            os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS shown (
                    scope TEXT NOT NULL, uid TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','sent','blocked')),
                    token TEXT, created REAL NOT NULL,
                    PRIMARY KEY(scope,uid)
                );
                CREATE INDEX IF NOT EXISTS shown_token ON shown(token);
                CREATE TABLE IF NOT EXISTS failed (
                    scope TEXT NOT NULL, uid TEXT NOT NULL, retry_at REAL NOT NULL,
                    PRIMARY KEY(scope,uid)
                );
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
            ''')
            if self.legacy_path and not db.execute(
                    "SELECT 1 FROM metadata WHERE key='legacy_imported'").fetchone():
                # No channel information exists in the old JSON. Conservatively
                # block its known IDs in every channel; NEVER silently drop history.
                if os.path.exists(self.legacy_path):
                    with open(self.legacy_path, encoding='utf-8') as f:
                        data = json.load(f)
                    if not isinstance(data, dict) or any(not isinstance(v, list) for v in data.values()):
                        raise ValueError('Повреждена старая история; автоматический сброс запрещён')
                    uids = {str(uid) for values in data.values() for uid in values}
                    with db:
                        db.executemany('INSERT OR IGNORE INTO shown VALUES (?,?,\'sent\',NULL,?)',
                                       [(LEGACY_SCOPE, uid, time.time()) for uid in uids])
                        db.execute("INSERT INTO metadata VALUES ('legacy_imported','1')")
                    logger.info('Импортировано %s ID из старой истории (общий запрет)', len(uids))
        except BaseException:
            db.close()
            raise
        self._db = db
        return db

    def recent(self, scope: str) -> set[str]:
        db = self._connection()
        return {row[0] for row in db.execute('''
            SELECT uid FROM shown WHERE scope IN (?,?)
            UNION SELECT uid FROM failed WHERE scope=? AND retry_at>?
        ''', (scope, LEGACY_SCOPE, scope, time.time()))}

    def _claim(self, scope: str, uids: Iterable[str], token: str) -> bool:
        ids = sorted(set(uids))
        if not ids:
            raise ValueError('Нельзя зарезервировать арт без идентификатора')
        db = self._connection()
        db.execute('BEGIN IMMEDIATE')
        try:
            for uid in ids:
                if db.execute('''SELECT 1 FROM shown WHERE uid=? AND scope IN (?,?)
                                 AND (token IS NULL OR token!=?) LIMIT 1''',
                              (uid, scope, LEGACY_SCOPE, token)).fetchone():
                    db.rollback()
                    return False
                if db.execute('SELECT 1 FROM failed WHERE scope=? AND uid=? AND retry_at>?',
                              (scope, uid, time.time())).fetchone():
                    db.rollback()
                    return False
            db.executemany("INSERT OR IGNORE INTO shown VALUES (?,?,'pending',?,?)",
                           [(scope, uid, token, time.time()) for uid in ids])
            db.commit()
            return True
        except BaseException:
            db.rollback()
            raise

    def reserve(self, scope: str, uids: Iterable[str]) -> str | None:
        token = uuid.uuid4().hex
        return token if self._claim(scope, uids, token) else None

    def extend(self, scope: str, token: str, uids: Iterable[str]) -> bool:
        return self._claim(scope, uids, token)

    def mark_sent(self, token: str) -> None:
        db = self._connection()
        with db:
            db.execute("UPDATE shown SET status='sent',token=NULL WHERE token=?", (token,))

    def mark_blocked(self, token: str) -> None:
        # Different post IDs may refer to bytes already sent/reserved elsewhere.
        db = self._connection()
        with db:
            db.execute("UPDATE shown SET status='blocked',token=NULL WHERE token=?", (token,))

    def release(self, token: str) -> None:
        db = self._connection()
        with db:
            db.execute("DELETE FROM shown WHERE token=? AND status='pending'", (token,))

    def failed(self, scope: str, uids: Iterable[str], ttl: float = 300) -> None:
        """Не застреваем на одних и тех же восьми битых/слишком больших файлах."""
        db = self._connection()
        with db:
            db.execute('DELETE FROM failed WHERE retry_at<=?', (time.time(),))
            db.executemany('INSERT OR REPLACE INTO failed VALUES (?,?,?)',
                           [(scope, uid, time.time() + ttl) for uid in set(uids)])

    def remember(self, scope: str, uid: str) -> None:
        db = self._connection()
        with db:
            db.execute("INSERT OR IGNORE INTO shown VALUES (?,?,'sent',NULL,?)",
                       (scope, uid, time.time()))

    def save(self) -> None:
        # Compatibility: every mutation is already committed, no periodic JSON write.
        if self._db is not None:
            self._db.commit()

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None
