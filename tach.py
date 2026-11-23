"""Read the same public profile and counters as a Tach guest."""
import json
import re
from urllib.parse import quote, urljoin, urlparse
from uuid import UUID

import requests

TACH = "https://tach.id"
NICK_RE = re.compile(r"^[A-Za-z0-9_.-]{2,50}$")
def extract_nickname(value):
    """Tach public profile URLs are /u/<nickname>; also accept plain @nick."""
    value = (value or '').strip()
    if len(value) > 240:
        raise ValueError('Слишком длинное имя профиля')
    if value.startswith(('https://', 'http://')):
        parsed = urlparse(value)
        if parsed.hostname not in ('tach.id', 'www.tach.id'):
            raise ValueError('Нужна ссылка именно на tach.id')
        parts = [p for p in parsed.path.split('/') if p]
        if not parts:
            raise ValueError('В ссылке нет ника')
        if parts[0].lower() in ('u', 'user', 'profile', 'p'):
            if len(parts) < 2:
                raise ValueError('В ссылке нет ника автора')
            value = parts[1]
        else:
            value = parts[0]
    value = value.lstrip('@').strip()
    if not NICK_RE.fullmatch(value):
        raise ValueError('Укажи ник Tach: латинские буквы, цифры, точка, _ или -')
    return value


def _profile_name(data):
    if not isinstance(data, dict):
        return ''
    for source in (data, data.get('user'), data.get('profile'), data.get('data')):
        if isinstance(source, dict):
            for key in ('nickname', 'nick_name', 'username', 'user_name', 'slug', 'nick'):
                v = source.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip().lstrip('@').lower()
    return ''


def extract_cover(item, content):
    """Find a publicly available preview URL in Tach content variants."""
    def url_from(value, depth=0):
        if depth > 4 or value is None:
            return None
        if isinstance(value, str):
            v = value.strip()
            if v.startswith(('https://', 'http://', '/')) and not v.startswith('//'):
                return v
            return None
        if isinstance(value, list):
            for obj in value[:8]:
                result = url_from(obj, depth + 1)
                if result:
                    return result
            return None
        if isinstance(value, dict):
            for key in ('cover_url', 'preview_url', 'thumbnail_url', 'poster_url',
                        'image_url', 'url', 'src', 'preview', 'thumbnail', 'cover',
                        'poster', 'image', 'original', 'medium', 'small', 'webp'):
                if key in value:
                    result = url_from(value[key], depth + 1)
                    if result:
                        return result
        return None

    for obj in (content, item):
        for key in ('cover_url', 'preview_url', 'thumbnail_url', 'poster_url',
                    'cover', 'preview', 'thumbnail', 'images_ext', 'images',
                    'media', 'video', 'attachments', 'files'):
            if isinstance(obj, dict) and key in obj:
                result = url_from(obj[key])
                if result:
                    return urljoin(TACH, result) if result.startswith('/') else result
    return None



class PublicTach:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({'Accept': 'application/json', 'X-App-Version': 'Tach/1.16.3 (WEB; 1; prod)'})
        self.token = None

    def get(self, path, params=None):
        for attempt in range(2):
            if not self.token:
                response = self.session.get(TACH + '/api/guest-token', timeout=(5, 15))
                response.raise_for_status()
                self.token = response.json()['token']
            response = self.session.get(TACH + path, params=params, timeout=(5, 15),
                                        headers={'Authorization': 'Bearer ' + self.token})
            if response.status_code == 401 and attempt == 0:
                self.token = None
                continue
            response.raise_for_status()
            return response.json()

    def resolve(self, name):
        data = self.get('/api/v1/user/' + quote(name, safe='') + '/')
        if _profile_name(data) != name.lower():
            raise ValueError('Tach не подтвердил ник профиля.')
        return str(UUID(data['id']))

    def posts(self, uid, full):
        from datetime import datetime
        posts = {}
        for kind in (4, 5, 6):
            page = 1
            while True:
                data = self.get('/api/v1/usercontent/', {
                    'page_size': 30, 'page': page, 'order': '-published',
                    'filter': json.dumps({'user': uid, 'type__in': [kind]}, separators=(',', ':'))})
                rows = data['results']
                for row in rows:
                    if str(row.get('user_id')) != uid or not row.get('published'):
                        continue
                    content = row.get('content') or {}
                    pid = str(UUID(row['id']))
                    description = str(content.get('description') or content.get('text') or '')
                    posts[pid] = dict(id=pid, type=str(kind), title=str(content.get('title') or description[:110] or 'Публикация'),
                                      description=description[:5000], published=datetime.fromisoformat(row['published'].replace('Z','+00:00')),
                                      duration=content.get('duration') or 0, cover=extract_cover(row, content),
                                      url=urljoin(TACH, row.get('url') or ''))
                if not full or len(rows) < 30 or page >= data['pagination']['page_count']:
                    break
                page += 1
        return sorted(posts.values(), key=lambda post: post['published'], reverse=True)

    def counts(self, pid):
        data = self.get('/api/v1/counters/usercontent/' + pid + '/')
        keys = ('content_viewing', 'content_like_like', 'content_comments')
        values = [data.get(k) for k in keys]
        if any(type(v) is not int or v < 0 for v in values):
            raise ValueError('Tach не прислал полный набор основных счётчиков.')
        shares = data.get('content_reposts', data.get('content_shares'))
        return (*values, shares if type(shares) is int and shares >= 0 else None)
