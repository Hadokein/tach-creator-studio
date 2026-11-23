import csv
import io
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
import requests
from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context

from store import connect, initialize, track
from tach import PublicTach, extract_nickname

app = Flask(__name__)
ROOT = Path(__file__).resolve().parent
TZ = ZoneInfo('Europe/Moscow')
DAY = "(stamp AT TIME ZONE 'Europe/Moscow')::date"
LATEST = """SELECT DISTINCT ON (id) id,views,likes,comments,shares,stamp
            FROM samples WHERE uid=%s ORDER BY id,stamp DESC"""

def serialized(row):
    return {k: v.isoformat() if hasattr(v, 'isoformat') else v for k,v in row.items()}

@app.errorhandler(psycopg.Error)
@app.errorhandler(RuntimeError)
def database_error(error):
    app.logger.error('Database: %s',type(error).__name__)
    return jsonify(error='База недоступна. Проверь DATABASE_URL и подключение к Postgres.'),503

@app.get('/')
def home():
    return send_from_directory(ROOT,'index.html')

@app.get('/api/health')
def health():
    return jsonify(ok=True,version='4.4')

@app.before_request
def prepare():
    if request.path.startswith(('/api/profiles','/api/analytics')):
        initialize()

@app.post('/api/profiles/track')
def register():
    if not request.is_json:
        return jsonify(error='Требуется JSON.'),415
    try:
        name = extract_nickname((request.get_json() or {}).get('profile')).lower()
        with connect() as db:
            known = db.execute('SELECT nick FROM tracked WHERE nick=%s',(name,)).fetchone()
        if not known:
            track(name, PublicTach().resolve(name))
        return jsonify(nickname=name,verified=True)
    except (ValueError,AttributeError) as exc:
        return jsonify(error=str(exc)),400
    except OverflowError as exc:
        return jsonify(error=str(exc)),409
    except requests.HTTPError as exc:
        if exc.response.status_code == 404:
            return jsonify(error='Профиль не найден в Tach.'),404
        return jsonify(error='Tach временно не подтвердил профиль.'),502
    except (requests.RequestException, KeyError):
        return jsonify(error='Не удалось получить публичный профиль Tach.'),502

def selected(db):
    return db.execute('SELECT * FROM tracked WHERE nick=%s',
                      (request.args.get('profile','vladislove').lower(),)).fetchone()

@app.get('/api/analytics')
def analytics():
    days = request.args.get('days',30,type=int)
    if days not in (7,30,90,365):
        return jsonify(error='Неверный период'),400
    start = datetime.now(TZ).date()-timedelta(days=days-1)
    cutoff = datetime.combine(start,datetime.min.time(),TZ)
    data = dict(days=days,profile=request.args.get('profile','vladislove'), tracked=False,
                timezone='Europe/Moscow',items=[],totals=dict.fromkeys(('views','likes','comments','shares')),
                daily_growth=[],hourly_growth=[],followers_history=[],followers=None,leaders=[])
    with connect() as db:
        db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        profile = selected(db)
        if not profile:
            return jsonify(data)
        uid = profile['uid']
        items = db.execute(f"""SELECT p.id,p.title,p.type,p.published,p.cover,p.url,
            s.views,s.likes,s.comments,s.shares,s.stamp AS observed_at
            FROM posts p LEFT JOIN ({LATEST}) s USING(id)
            WHERE p.uid=%s ORDER BY p.published DESC""",(uid,uid)).fetchall()
        for metric in data['totals']:
            values = [i[metric] for i in items if i[metric] is not None]
            if values and (metric != 'shares' or len(values)==len(items)):
                data['totals'][metric] = sum(values)
        daily = db.execute(f"""SELECT {DAY} AS date,
            COALESCE(sum(greatest(dv,0)),0)::bigint AS views,
            COALESCE(sum(greatest(dl,0)),0)::bigint AS likes,
            COALESCE(sum(greatest(dc,0)),0)::bigint AS comments,
            count(distinct id) FILTER (WHERE prev IS NOT NULL) AS comparable_posts,
            count(distinct id) FILTER (WHERE prev IS NULL) AS new_posts,
            count(distinct id) FILTER (WHERE stamp-prev > interval '25 hours') AS gap_posts,
            count(distinct id) FILTER (WHERE least(dv,dl,dc)<0) AS reset_posts
            FROM samples WHERE uid=%s AND stamp>=%s GROUP BY 1 ORDER BY 1""",(uid,cutoff)).fetchall()
        hourly = db.execute("""SELECT date_trunc('hour',stamp AT TIME ZONE 'Europe/Moscow') AS date,
            sum(greatest(0,dv))::bigint AS views FROM samples
            WHERE uid=%s AND stamp>=%s AND prev IS NOT NULL GROUP BY 1 ORDER BY 1""",(uid,cutoff)).fetchall()
        audience = db.execute(f"""SELECT DISTINCT ON ({DAY}) {DAY} AS date,followers FROM audience
            WHERE uid=%s AND stamp >=%s ORDER BY {DAY},stamp DESC""",(uid,cutoff)).fetchall()
        last = db.execute('SELECT followers FROM audience WHERE uid=%s ORDER BY stamp DESC LIMIT 1',(uid,)).fetchone()
        first = db.execute('SELECT min(stamp) AS stamp FROM samples WHERE uid=%s',(uid,)).fetchone()['stamp']
        leaders = db.execute("""SELECT p.id,p.title,sum(greatest(s.dv,0))::bigint AS gain
            FROM samples s JOIN posts p USING(uid,id) WHERE uid=%s AND stamp>=%s AND dv>0
            GROUP BY p.id,p.title ORDER BY gain DESC,p.title LIMIT 5""",(uid,cutoff)).fetchall()
        data.update(tracked=True, items=[serialized(i) for i in items],daily_growth=[serialized(i) for i in daily],
                    hourly_growth=[serialized(i) for i in hourly],followers_history=[serialized(i) for i in audience],
                    followers=last['followers'] if last else None,leaders=leaders,
                    refresh_error=profile['warning'],last_observed_at=serialized(profile)['last_run'],
                    first_observed_at=first.isoformat() if first else None)
    return jsonify(data)

@app.get('/api/analytics/post/<uuid:pid>')
def details(pid):
    with connect() as db:
        profile = selected(db)
        args = (profile['uid'] if profile else '',str(pid))
        post = db.execute('SELECT * FROM posts WHERE uid=%s AND id=%s',args).fetchone()
        if not post:
            return jsonify(error='Публикация не найдена'),404
        rows = db.execute(f"""SELECT {DAY} AS day, stamp,views,likes,comments,dv,dl,dc,prev
            FROM samples WHERE uid=%s AND id=%s ORDER BY stamp""",args)
        history = {}
        for row in rows:
            day = row['day'].isoformat()
            entry = history.setdefault(day,dict(date=day,total={},growth=None,reset=False,days_between=None))
            entry['total'] = {k:row[k] for k in ('views','likes','comments')}
            if row['prev']:
                gap = (row['day'] - row['prev'].astimezone(TZ).date()).days
                entry['days_between'] = max(entry['days_between'] or 0,gap)
                if entry['growth'] is None:
                    entry['growth'] = dict(views=0,likes=0,comments=0)
                for key,col in zip(entry['growth'],('dv','dl','dc')):
                    entry['reset'] |= row[col] < 0
                    entry['growth'][key] += max(0,row[col])
        return jsonify(post=serialized(dict(post,author=profile['nick'])),history=list(history.values()))

@app.get('/api/analytics/export.csv')
def export():
    nick = request.args.get('profile','vladislove').lower()
    @stream_with_context
    def generate():
        out = io.StringIO()
        writer = csv.writer(out)
        yield '\ufeffДата и время,Название,ID,Просмотры,Лайки,Комментарии,Репосты\r\n'
        with connect() as db, db.cursor(name='export_samples') as cursor:
            cursor.execute("""SELECT stamp,title,id,views,likes,comments,shares FROM samples
                JOIN posts USING(uid,id) JOIN tracked USING(uid) WHERE nick=%s ORDER BY stamp DESC""",(nick,))
            for row in cursor:
                title = row['title'] or ''
                if title.lstrip().startswith(('=','+','-','@')): title = "'" + title
                writer.writerow([row['stamp'].astimezone(TZ).isoformat(),title,row['id'],row['views'],row['likes'],row['comments'],row['shares']])
                yield out.getvalue()
                out.seek(0)
                out.truncate()
    return Response(generate(),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=tach_history.csv'})

@app.get('/history.js')
def device_history_script():
    return send_from_directory(ROOT,'history.js')
