"""
Minimal first-party request/event logging to SQLite. No third-party service,
near-zero memory footprint (sqlite3 is stdlib) - picked deliberately for a
1GB box. Logs every request (path, method, status, latency, caller IP) plus
board-specific events (generate/preview with component count) so we can
answer "who's hitting what" and "how many boards made" without Umami/GA.
"""
import os
import sqlite3
import time
from contextlib import contextmanager

DB_PATH = os.environ.get('GROOVEBOARD_LOG_DB',
                          os.path.join(os.path.dirname(os.path.abspath(__file__)), 'access.db'))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    status INTEGER NOT NULL,
    duration_ms REAL NOT NULL,
    ip TEXT,
    user_agent TEXT,
    referer TEXT
);
CREATE TABLE IF NOT EXISTS board_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    event TEXT NOT NULL,          -- 'generate' | 'preview' | 'preview_parts'
    board_name TEXT,
    n_components INTEGER,
    n_connections INTEGER,
    ip TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts);
CREATE INDEX IF NOT EXISTS idx_events_ts ON board_events(ts);
"""


@contextmanager
def _conn():
    # short-lived connection per call; sqlite handles concurrent readers fine
    # and writes here are infrequent/small, so no pooling needed.
    c = sqlite3.connect(DB_PATH, timeout=5)
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db():
    with _conn() as c:
        c.executescript(_SCHEMA)


def log_request(method, path, status, duration_ms, ip, user_agent, referer):
    with _conn() as c:
        c.execute(
            'INSERT INTO requests (ts, method, path, status, duration_ms, ip, user_agent, referer) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (time.time(), method, path, status, duration_ms, ip, user_agent, referer),
        )


def log_board_event(event, board_name, n_components, n_connections, ip):
    with _conn() as c:
        c.execute(
            'INSERT INTO board_events (ts, event, board_name, n_components, n_connections, ip) '
            'VALUES (?,?,?,?,?,?)',
            (time.time(), event, board_name, n_components, n_connections, ip),
        )


def stats_summary(since_ts=0.0):
    with _conn() as c:
        c.row_factory = sqlite3.Row
        total_requests = c.execute(
            'SELECT COUNT(*) n FROM requests WHERE ts >= ?', (since_ts,)).fetchone()['n']
        unique_ips = c.execute(
            'SELECT COUNT(DISTINCT ip) n FROM requests WHERE ts >= ?', (since_ts,)).fetchone()['n']
        top_paths = c.execute(
            'SELECT path, COUNT(*) n FROM requests WHERE ts >= ? '
            'GROUP BY path ORDER BY n DESC LIMIT 20', (since_ts,)).fetchall()
        events = c.execute(
            'SELECT event, COUNT(*) n FROM board_events WHERE ts >= ? '
            'GROUP BY event ORDER BY n DESC', (since_ts,)).fetchall()
        recent_boards = c.execute(
            'SELECT ts, event, board_name, n_components, n_connections FROM board_events '
            'WHERE ts >= ? ORDER BY ts DESC LIMIT 50', (since_ts,)).fetchall()
        return {
            'total_requests': total_requests,
            'unique_ips': unique_ips,
            'top_paths': [dict(r) for r in top_paths],
            'board_events': [dict(r) for r in events],
            'recent_boards': [dict(r) for r in recent_boards],
        }
