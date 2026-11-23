"""Public collection with a local SQLite spool and browser-side history."""
import gzip
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from functools import lru_cache

from flask import Flask, jsonify, request, send_from_directory
import requests

from tach import PublicTach, extract_nickname

ROOT = Path(__file__).resolve().parent
DB = Path(os.getenv('TACH_LOCAL_DB', '/tmp/tach-studio-45.sqlite3'))
DEFAULT = 'vladislove'
SEED = ROOT / 'history.json.gz'
app = Flask(__name__)
log = logging.getLogger('tach.public')

def connection():
    db = sqlite3.connect(DB,timeout=10)
    db.row_factory = sqlite3.Row
    db.executescript("""
    CREATE TABLE IF NOT EXISTS profiles(nick TEXT PRIMARY KEY,uid TEXT NOT NULL,attempt REAL DEFAULT 0,
        done REAL DEFAULT 0,full_day TEXT,lease REAL DEFAULT 0,error TEXT);
    CREATE TABLE IF NOT EXISTS batches(nick TEXT, stamp TEXT, payload TEXT, PRIMARY KEY(nick,stamp));
    """)
    return db

@lru_cache(maxsize=1)
def seed():
    if not SEED.is_file():
        return {}
    with gzip.open(SEED,'rt',encoding='utf8') as f:
        return json.load(f)

def collect_batch(nick,uid,full,client=None):
    client = client or PublicTach()
    posts = client.posts(uid, full)
    samples = []
    missed = 0
    for p in posts if full else posts[:10]:
        try:
            values = client.counts(p['id'])
            samples.append([p['id'], datetime.now(timezone.utc).isoformat(timespec='milliseconds'), *values])
        except (ValueError,requests.RequestException) as exc:
            if getattr(getattr(exc,'response',None),'status_code',None)==429:
                raise
            missed += 1
    profile = client.get('/api/v1/counters/user/'+uid+'/')
    if posts and not samples:
        raise ValueError('Нет счётчиков')
    for p in posts:
        p['published'] = p['published'].isoformat()
    followers = profile.get('followers_user')
    return dict(nick=nick,uid=uid,stamp=datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
                items=posts,samples=samples,followers=followers if type(followers) is int else None,missed=missed)

def ensure(nick,uid):
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM profiles WHERE nick=?',(nick,)).fetchone():
            return
        if db.execute('SELECT count(*) FROM profiles').fetchone()[0]>=int(os.getenv('MAX_PROFILES','3')):
            raise ValueError('Все места пилота заняты.')
        db.execute('INSERT INTO profiles(nick,uid) VALUES(?,?)',(nick,uid))

def work(nick,uid,full,lease):
    try:
        batch=collect_batch(nick,uid,full)
        now=time.time()
        with connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM profiles WHERE nick=? AND lease=?',(nick,lease)).fetchone():
                return
            db.execute('INSERT OR IGNORE INTO batches VALUES(?,?,?)',(nick,batch['stamp'],json.dumps(batch,ensure_ascii=False)))
            db.execute('DELETE FROM batches WHERE nick=? AND stamp < ?',(nick,datetime.fromtimestamp(now-32*86400,timezone.utc).isoformat()))
            db.execute("""UPDATE profiles SET done=?,lease=0,error=?,full_day=CASE WHEN ? THEN ? ELSE full_day END
                          WHERE nick=?""",(now,f"Без замера: {batch['missed']} публикаций." if batch['missed'] else None,
                                          full,batch['stamp'][:10],nick))
    except Exception as exc:
        log.warning('Сбор @%s: %s',nick,type(exc).__name__)
        with connection() as db:
            db.execute('UPDATE profiles SET lease=0,error=? WHERE nick=? AND lease=?',
                       ('Tach временно не прислал данные. Повторим через несколько минут.',nick,lease))

def schedule(nick):
    now=time.time()
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        p=db.execute('SELECT * FROM profiles WHERE nick=?',(nick,)).fetchone()
        if not p or p['lease']>now or now-p['attempt']<(300 if p['error'] else 3600):
            return
        lease=now+1800
        db.execute('UPDATE profiles SET attempt=?,lease=? WHERE nick=?',(now,lease,nick))
    full=p['full_day']!=datetime.now(timezone.utc).date().isoformat()
    threading.Thread(target=work,args=(nick,p['uid'],full,lease),daemon=True).start()

@app.after_request
def compress_history(response):
    if request.path=='/api/analytics' and response.status_code==200:
        response.headers['Cache-Control']='no-store'
        response.vary.add('Accept-Encoding')
        if request.accept_encodings['gzip'] and len(response.get_data())>4096:
            response.set_data(gzip.compress(response.get_data(),compresslevel=4))
            response.headers['Content-Encoding']='gzip'
    return response

@app.get('/')
def home():
    return send_from_directory(ROOT,'index.html')

@app.get('/history.js')
def script():
    return send_from_directory(ROOT,'history.js')

@app.get('/api/health')
def health():
    return jsonify(ok=True,version='4.5',mode='standalone')

@app.post('/api/profiles/track')
def track():
    try:
        nick=extract_nickname((request.get_json() or {}).get('profile')).lower()
        with connection() as db:
            p=db.execute('SELECT uid FROM profiles WHERE nick=?',(nick,)).fetchone()
        if not p:
            ensure(nick,PublicTach().resolve(nick))
        schedule(nick)
        return jsonify(verified=True,nickname=nick)
    except (AttributeError,ValueError) as exc:
        return jsonify(error=str(exc)),400
    except requests.HTTPError as exc:
        return jsonify(error='Профиль не найден или Tach временно недоступен.'),404 if exc.response.status_code==404 else 502
    except (KeyError,requests.RequestException):
        return jsonify(error='Не удалось проверить профиль Tach.'),502

@app.get('/api/analytics')
def analytics():
    try:
        nick=extract_nickname(request.args.get('profile',DEFAULT)).lower()
    except ValueError as exc:
        return jsonify(error=str(exc)),400
    with connection() as db:
        p=db.execute('SELECT * FROM profiles WHERE nick=?',(nick,)).fetchone()
    if not p and nick==DEFAULT:
        ensure(nick,'7fd088f2-79a4-4ec4-a2a1-f1b45724fb99')
    cursor=request.args.get('since','')
    baseline=seed().get(nick,{})
    if not p and baseline:
        ensure(nick,baseline['uid'])
    schedule(nick)
    with connection() as db:
        p=db.execute('SELECT * FROM profiles WHERE nick=?',(nick,)).fetchone()
        batches=[json.loads(r[0]) for r in db.execute(
            'SELECT payload FROM batches WHERE nick=? AND stamp>? ORDER BY stamp',(nick,cursor))]
    seed_version=baseline.get('batches',[])[-1]['stamp'] if baseline.get('batches') else ''
    initial=baseline.get('batches',[]) if request.args.get('seed')!=seed_version else []
    return jsonify(mode='device',profile=nick,uid=p['uid'] if p else None,tracked=bool(p),
                   pending=bool(p and p['lease']>time.time()),refresh_error=p['error'] if p else None,
                   seed_version=seed_version,batches=initial+batches)
