import os
from functools import lru_cache

import psycopg
from psycopg.rows import dict_row

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracked (
 uid text PRIMARY KEY, nick text UNIQUE NOT NULL, last_run timestamptz,
 last_full date, warning text, revision bigint DEFAULT 0 NOT NULL);
CREATE TABLE IF NOT EXISTS posts (
 uid text REFERENCES tracked, id text NOT NULL, title text, type text, published timestamptz,
 description text, duration double precision, cover text, url text, PRIMARY KEY(uid,id));
CREATE TABLE IF NOT EXISTS samples (
 uid text, id text, stamp timestamptz, views bigint, likes bigint, comments bigint, shares bigint,
 prev timestamptz, dv bigint, dl bigint, dc bigint,
 PRIMARY KEY(uid,id,stamp), FOREIGN KEY(uid,id) REFERENCES posts);
CREATE INDEX IF NOT EXISTS samples_period ON samples(uid,stamp);
CREATE TABLE IF NOT EXISTS audience (
 uid text REFERENCES tracked, stamp timestamptz, followers bigint, PRIMARY KEY(uid,stamp));
"""

def connect():
    url = os.environ.get('DATABASE_URL')
    if not url:
        raise RuntimeError('Задай DATABASE_URL для подключения к Postgres.')
    return psycopg.connect(url, connect_timeout=10, row_factory=dict_row)

@lru_cache(maxsize=1)
def initialize():
    with connect() as db:
        db.execute('SELECT pg_advisory_xact_lock(417562915)')
        db.execute(SCHEMA)

def track(nick, uid):
    with connect() as db:
        db.execute('LOCK TABLE tracked IN SHARE ROW EXCLUSIVE MODE')
        if db.execute('SELECT uid FROM tracked WHERE uid=%s', (uid,)).fetchone():
            db.execute('UPDATE tracked SET nick=%s WHERE uid=%s', (nick, uid))
            return
        count = db.execute('SELECT count(*) AS n FROM tracked').fetchone()['n']
        if count >= int(os.getenv('MAX_PROFILES','3')):
            raise OverflowError('Все места пилота заняты. Уже добавленные профили доступны.')
        db.execute('INSERT INTO tracked(uid,nick) VALUES(%s,%s)', (uid,nick))
