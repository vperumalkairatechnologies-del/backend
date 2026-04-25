"""
config/db.py — Thread-safe MySQL connection pool.

Replaces the old singleton _connection which was NOT thread-safe and could
corrupt queries under concurrent Flask requests.

Usage (unchanged across the codebase):
    from config.db import get_db
    db = get_db()
    with db.cursor() as cur:
        cur.execute(...)
    db.close()   # returns connection back to pool — does NOT close it
"""

import os
import logging
import pymysql
import pymysql.cursors
from threading import Lock

logger = logging.getLogger(__name__)

_pool: list = []
_pool_lock = Lock()
_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "10"))


def _new_connection() -> pymysql.connections.Connection:
    return pymysql.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "3306")),
        db=os.getenv("DB_NAME", "smartcard"),
        user=os.getenv("DB_USER", "root"),
        password=os.getenv("DB_PASS", ""),   # ← no hardcoded default
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
        connect_timeout=10,
    )


class _PooledConnection:
    """
    Thin wrapper: db.close() returns the connection to the pool
    instead of destroying it — identical call-site API to before.
    """

    def __init__(self, conn: pymysql.connections.Connection):
        self._conn = conn

    def cursor(self):
        return self._conn.cursor()

    def autocommit(self, val: bool):
        self._conn.autocommit(val)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    @property
    def open(self):
        return self._conn.open

    def close(self):
        """Return to pool instead of closing."""
        try:
            self._conn.ping(reconnect=False)
            self._conn.autocommit(True)  # reset autocommit before returning to pool
            alive = True
        except Exception:
            alive = False

        with _pool_lock:
            if alive and len(_pool) < _POOL_SIZE:
                _pool.append(self._conn)
            else:
                try:
                    self._conn.close()
                except Exception:
                    pass


def get_db() -> _PooledConnection:
    """
    Acquire a pooled connection. Always call db.close() when done
    so the connection is returned to the pool for the next request.
    """
    with _pool_lock:
        while _pool:
            conn = _pool.pop()
            try:
                conn.ping(reconnect=True)
                return _PooledConnection(conn)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass

    # Pool empty — open a fresh connection
    try:
        conn = _new_connection()
        return _PooledConnection(conn)
    except Exception as exc:
        logger.critical("Cannot connect to MySQL: %s", exc)
        raise
