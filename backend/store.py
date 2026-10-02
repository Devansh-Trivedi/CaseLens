"""Private local storage for this single-user hackathon app."""
import json
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / '.local'
DATA.mkdir(mode=0o700, exist_ok=True)
DB = DATA / 'caselens.sqlite3'


def connection():
    con = sqlite3.connect(DB, timeout=20)
    con.execute('CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    os.chmod(DB, 0o600)
    return con


def get(key, default=None):
    with connection() as con:
        row = con.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def put(key, value):
    with connection() as con:
        con.execute('INSERT OR REPLACE INTO kv VALUES (?,?)', (key, json.dumps(value)))


def remove(key):
    with connection() as con:
        con.execute('DELETE FROM kv WHERE key=?', (key,))
