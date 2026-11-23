"""Scheduled public measurements, separate from the Render web process."""
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

from store import connect, initialize
from tach import PublicTach

MOSCOW = ZoneInfo('Europe/Moscow')
log = logging.getLogger('collector')

def now():
    return datetime.now(timezone.utc)

def collect(profile, client):
    uid = profile['uid']
    day = now().astimezone(MOSCOW).date()
    full = profile['last_full'] != day
    posts = client.posts(uid, full)
    measurements = []
    missed = 0
    for post in posts if full else posts[:10]:
        try:
            values = client.counts(post['id'])
            measurements.append((post['id'], now(), values))
        except (requests.RequestException, ValueError) as exc:
            if getattr(getattr(exc, 'response', None), 'status_code', None) == 429:
                raise
            missed += 1
    profile_counts = client.get('/api/v1/counters/user/' + uid + '/')
    followers = profile_counts.get('followers_user')
    warning = f'Без полного замера: {missed} публикаций.' if missed else None
    if posts and not measurements:
        raise ValueError('Tach не вернул счётчики публикаций.')
    with connect() as db:
        current = db.execute('SELECT revision FROM tracked WHERE uid=%s FOR UPDATE',(uid,)).fetchone()
        if current['revision'] != profile['revision']:
            return True
        for p in posts:
            db.execute("""INSERT INTO posts(uid,id,title,type,published,description,duration,cover,url)
                VALUES(%(uid)s,%(id)s,%(title)s,%(type)s,%(published)s,%(description)s,%(duration)s,%(cover)s,%(url)s)
                ON CONFLICT(uid,id) DO UPDATE SET title=excluded.title, published=excluded.published,
                type=excluded.type, description=excluded.description, duration=excluded.duration,
                cover=excluded.cover,url=excluded.url""", dict(uid=uid, **p))
        for pid, stamp, values in measurements:
            prior = db.execute('SELECT * FROM samples WHERE uid=%s AND id=%s ORDER BY stamp DESC LIMIT 1',
                               (uid,pid)).fetchone()
            differences = [values[i]-prior[k] if prior else None for i,k in enumerate(('views','likes','comments'))]
            db.execute('INSERT INTO samples VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                       (uid,pid,stamp,*values,prior['stamp'] if prior else None,*differences))
        if type(followers) is int and followers >= 0:
            db.execute('INSERT INTO audience VALUES(%s,%s,%s)',(uid,now(),followers))
        db.execute('UPDATE tracked SET revision=revision+1,last_run=%s,last_full=%s,warning=%s WHERE uid=%s',
                   (now(),day if full else profile['last_full'],warning,uid))
    return not missed

def main():
    initialize()
    with connect() as db:
        profiles = db.execute('SELECT * FROM tracked ORDER BY nick').fetchall()
    failed = False
    for profile in profiles:
        try:
            if not collect(profile, PublicTach()):
                failed = True
        except Exception as exc:
            failed = True
            log.error('Сбор @%s: %s',profile['nick'],type(exc).__name__)
            with connect() as db:
                db.execute('UPDATE tracked SET warning=%s WHERE uid=%s AND revision=%s',
                           ('Сбор прерван. Показан последний сохранённый результат.',profile['uid'],profile['revision']))
    print(f'Проверено профилей: {len(profiles)}')
    return int(failed)

if __name__ == '__main__':
    raise SystemExit(main())
