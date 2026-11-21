import json
import base64
import time
import threading
import contextvars
import hashlib
import sqlite3
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import os
import re
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__)
BASE_DIR = Path(__file__).resolve().parent
TACH = 'https://tach.id'
USER_ID = '7fd088f2-79a4-4ec4-a2a1-f1b45724fb99'
NICKNAME = 'vladislove'
SAMPLE_CONTENT_ID = 'b3fd8adf-89c3-40a4-943f-8258b0225371'
PUBLIC_POST_PATH = '/p/vladislove/post/66ff2cb9'
VERSION = '3.5'
SESSION = requests.Session()
SESSION.headers.update({'Accept': 'application/json', 'User-Agent': 'TachCreatorStudio/3.0'})
APP_VERSION = 'Tach/1.16.3 (WEB; 1; prod)'
GUEST_LOCK = threading.RLock()
GUEST_CACHE = {'token': None, 'expires_at': 0}


def _jwt_exp(token):
    try:
        payload = token.split('.')[1]
        payload += '=' * (-len(payload) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload))
        exp = decoded.get('exp')
        return int(exp) if isinstance(exp, (int, float)) and not isinstance(exp, bool) else 0
    except (ValueError, IndexError, TypeError, UnicodeDecodeError):
        return 0


def get_guest_token(force=False):
    # Same public guest-token route used by Tach's browser client.
    with GUEST_LOCK:
        if (not force and GUEST_CACHE['token'] and
                GUEST_CACHE['expires_at'] > time.time() + 30):
            return GUEST_CACHE['token']
        response = SESSION.get(
            TACH + '/api/guest-token',
            headers={'Accept': 'application/json', 'X-App-Version': APP_VERSION},
            timeout=12,
        )
        response.raise_for_status()
        data = response.json()
        token = data.get('token') if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise ValueError('Guest token response is missing token')
        expiry = _jwt_exp(token)
        if expiry <= time.time() + 30:
            raise ValueError('Guest token is expired or invalid')
        GUEST_CACHE.update(token=token, expires_at=expiry)
        return token


def tach_api_get(path, params=None):
    token = get_guest_token()
    for attempt in range(2):
        response = SESSION.get(
            TACH + path, params=params, timeout=15,
            headers={
                'Authorization': 'Bearer ' + token,
                'X-App-Version': APP_VERSION,
                'Accept': 'application/json',
            },
        )
        if response.status_code == 401 and attempt == 0:
            token = get_guest_token(force=True)
            continue
        return response
    return response



def classify_error(response):
    content_type = response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
    body = response.text[:2048].lower()
    if 'no authorization header found' in body:
        message = 'no Authorization header found'
    elif 'unauthorized' in body:
        message = 'unauthorized'
    elif 'forbidden' in body:
        message = 'forbidden'
    elif 'access denied' in body:
        message = 'access denied'
    else:
        message = 'unrecognized response'
    return {'tach_status': response.status_code, 'tach_content_type': content_type, 'tach_message': message}


@app.get('/')
def home():
    if not (BASE_DIR / 'index.html').is_file():
        return jsonify({'error': 'index.html не найден рядом с app.py'}), 404
    return send_from_directory(str(BASE_DIR), 'index.html')


@app.get('/tach_browser_test.html')
def browser_test():
    if not (BASE_DIR / 'tach_browser_test.html').is_file():
        return jsonify({'error': 'tach_browser_test.html не найден рядом с app.py'}), 404
    return send_from_directory(str(BASE_DIR), 'tach_browser_test.html')


class PublicPageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title_active = False
        self.title = ''
        self.meta = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == 'title':
            self.title_active = True
        if tag == 'meta':
            key = attributes.get('property') or attributes.get('name')
            if key in ('description', 'og:title', 'og:description', 'og:image'):
                self.meta[key] = attributes.get('content', '')[:500]

    def handle_endtag(self, tag):
        if tag == 'title':
            self.title_active = False

    def handle_data(self, data):
        if self.title_active:
            self.title += data


@app.get('/api/public-page-probe')
def public_page_probe():
    """Read-only test: can Render fetch the public post HTML?"""
    url = TACH + PUBLIC_POST_PATH
    try:
        response = SESSION.get(url, timeout=15, headers={'Accept': 'text/html'})
        result = {
            'version': VERSION,
            'url': url,
            'status': response.status_code,
            'content_type': response.headers.get('Content-Type', '').split(';', 1)[0],
        }
        if not response.ok:
            result['message'] = classify_error(response)['tach_message']
            result['conclusion'] = 'Render cannot read this public page with the current request.'
            return jsonify(result)
        html = response.text
        parser = PublicPageParser()
        parser.feed(html)
        result.update({
            'title': parser.title.strip()[:200],
            'meta': parser.meta,
            'html_bytes': len(response.content),
            'next_data_present': '__next_f.push' in html or '__NEXT_DATA__' in html,
            'counter_fields_in_html': {
                key: key in html
                for key in ('content_viewing', 'content_like_like', 'content_comments')
            },
        })
        result['conclusion'] = (
            'Counter field names found in HTML; further structural parsing is needed.'
            if any(result['counter_fields_in_html'].values())
            else 'Public page accessible, but no API counter field names found in HTML.'
        )
        return jsonify(result)
    except requests.Timeout:
        return jsonify({'version': VERSION, 'error': 'timeout'})
    except requests.RequestException as exc:
        return jsonify({'version': VERSION, 'error': type(exc).__name__})


class ScriptInventoryParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag != 'script':
            return
        src = dict(attrs).get('src', '')
        if src.startswith('/_next/static/') and src.endswith('.js'):
            if src not in self.scripts:
                self.scripts.append(src)


@app.get('/api/source-inspect')
def source_inspect():
    """Read-only inspection of public HTML and a small number of public JS bundles.

    No upstream URLs or credentials accepted from the client. No raw JS or secrets returned.
    """
    try:
        page = SESSION.get(TACH + PUBLIC_POST_PATH, timeout=12,
                           headers={'Accept': 'text/html'})
        result = {'version': VERSION, 'page_status': page.status_code}
        if not page.ok:
            result['error'] = classify_error(page)['tach_message']
            return jsonify(result)
        html = page.text
        inv = ScriptInventoryParser()
        inv.feed(html)
        result['html'] = {
            'bytes': len(page.content),
            'next_f_blocks': html.count('self.__next_f.push'),
            'js_files_found': len(inv.scripts),
            'post_id_present': '66ff2cb9' in html,
            'content_uuid_present': SAMPLE_CONTENT_ID in html,
            'profile_uuid_present': USER_ID in html,
            'counter_names': {key: key in html for key in
                              ('content_viewing', 'content_like_like', 'content_comments')},
            'embedded_hints': {key: key in html for key in
                               ('userContent', 'usercontent', 'published', 'nickname', 'views', 'viewCount')},
        }
        # Scan a bounded subset, prioritizing route-specific and app bundles.
        scripts = sorted(inv.scripts, key=lambda src: (
            0 if '/app/' in src else 1 if 'main-app' in src else 2,
            src
        ))[:8]
        markers = ('counters/usercontent', 'content_viewing', 'authorization',
                   'bearer', '/api/v1/', 'usercontent')
        bundles = []
        for src in scripts:
            entry = {'file': src}
            try:
                js_url = urljoin(TACH, src)
                if urlparse(js_url).netloc != 'tach.id':
                    continue
                resp = SESSION.get(js_url, timeout=8,
                                   headers={'Accept': 'application/javascript'})
                entry['status'] = resp.status_code
                entry['bytes'] = len(resp.content)
                if resp.ok and len(resp.content) <= 5_000_000:
                    lower = resp.text.lower()
                    entry['markers'] = {m: lower.count(m) for m in markers}
                elif not resp.ok:
                    entry['message'] = classify_error(resp)['tach_message']
            except requests.RequestException as exc:
                entry['error'] = type(exc).__name__
            bundles.append(entry)
        result['bundles'] = bundles
        result['note'] = ('Marker counts identify candidate bundles, not the '
                          'authorization mechanism. No credentials are collected.')
        return jsonify(result)
    except requests.RequestException as exc:
        return jsonify({'version': VERSION, 'error': type(exc).__name__})


@app.get('/api/source-inspect-deep')
def source_inspect_deep():
    """Inspect public JS bundles in batches, returning limited contextual excerpts."""
    try:
        batch = int(request.args.get('batch', '1'))
    except ValueError:
        return jsonify({'error': 'batch must be 1 through 5'}), 400
    if batch not in range(1, 6):
        return jsonify({'error': 'batch must be 1 through 5'}), 400
    try:
        page = SESSION.get(TACH + PUBLIC_POST_PATH, timeout=12, headers={'Accept': 'text/html'})
        if not page.ok:
            return jsonify({'version': VERSION, 'page_status': page.status_code})
        inventory = ScriptInventoryParser()
        inventory.feed(page.text)
        files = inventory.scripts
        selected = files[(batch - 1) * 8:batch * 8]
        markers = ('usercontent', 'counter', 'authorization', 'bearer', 'api/v1',
                   'content_viewing', 'fetch(', 'axios', 'interceptor', 'token')
        entries = []
        for src in selected:
            entry = {'file': src}
            try:
                resp = SESSION.get(TACH + src, timeout=9)
                entry['status'] = resp.status_code
                entry['bytes'] = len(resp.content)
                if resp.ok and len(resp.content) <= 5_000_000:
                    lower = resp.text.lower()
                    entry['markers'] = {m: lower.count(m) for m in markers}
                    # Context only around non-sensitive routing and counter references.
                    # Do not return token values, authorization header contents or full JS.
                    excerpts = []
                    for marker in ('usercontent', 'counter', 'api/v1', 'content_viewing'):
                        start = 0
                        for _ in range(3):
                            idx = lower.find(marker, start)
                            if idx < 0:
                                break
                            fragment = resp.text[max(0, idx-100):idx+160]
                            fragment = re.sub(r'(?i)(bearer\s+)[a-z0-9._~+/-]+', r'\1[REDACTED]', fragment)
                            excerpts.append({'marker': marker, 'context': fragment})
                            start = idx + len(marker)
                    entry['excerpts'] = excerpts[:8]
            except requests.RequestException as exc:
                entry['error'] = type(exc).__name__
            entries.append(entry)
        return jsonify({'version': VERSION, 'batch': batch, 'total_files': len(files),
                        'files_in_batch': len(selected), 'bundles': entries,
                        'note': 'Public JS excerpts; no authentication secrets requested.'})
    except requests.RequestException as exc:
        return jsonify({'version': VERSION, 'error': type(exc).__name__})



@app.get('/api/auth-client-inspect')
def auth_client_inspect():
    """Inspect public Tach client implementation, without retrieving credentials."""
    targets = {
        'api_schema': '/_next/static/chunks/422-5df1ddcb9ef44d09.js',
        'http_library': '/_next/static/chunks/4385-e13dd9f6eb36281f.js',
        'counter_ui': '/_next/static/chunks/3943-eee345068b2fc289.js',
    }
    needles = {
        'counter_method': 'oneCountersUsercontentApiV1CountersUsercontentUsercontentIdGet',
        'counter_path': '/counters/usercontent/',
        'http_bearer_schema': 'HTTPBearer',
        'authorization_header': 'authorization',
        'axios_interceptor': 'interceptors.request',
        'access_token_refresh': 'getAccessTokenPayloadSecurely',
        'api_base_url': 'apiUrl',
    }
    out = {}
    for name, path in targets.items():
        try:
            response = SESSION.get(TACH + path, timeout=12)
            entry = {'status': response.status_code, 'bytes': len(response.content)}
            if response.ok and len(response.content) <= 2_000_000:
                source = response.text
                lower = source.lower()
                entry['matches'] = {}
                for label, needle in needles.items():
                    indices = [m.start() for m in list(re.finditer(re.escape(needle.lower()), lower))[:5]]
                    entry['matches'][label] = {'count': lower.count(needle.lower()), 'offsets': indices}
                # Only return narrowly scoped API route and method contexts.
                # No raw Authorization or token-bearing JavaScript is returned.
                contexts = []
                for label in ('counter_method', 'counter_path', 'http_bearer_schema'):
                    needle = needles[label]
                    pos = lower.find(needle.lower())
                    if pos >= 0:
                        fragment = source[max(0, pos - 100):pos + len(needle) + 180]
                        fragment = re.sub(r'(?i)(bearer\s+)[a-z0-9._~+/-]+', r'\1[REDACTED]', fragment)
                        fragment = re.sub(r'(?i)(authorization["\\\']?\s*[:=]\s*["\\\'])[^"\\\']+', r'\1[REDACTED]', fragment)
                        contexts.append({'kind': label, 'excerpt': fragment})
                entry['route_excerpts'] = contexts[:3]
            out[name] = entry
        except requests.RequestException as exc:
            out[name] = {'error': type(exc).__name__}
    return jsonify({
        'version': VERSION,
        'bundles': out,
        'interpretation': 'Presence of HTTPBearer is a schema declaration, not proof of required authentication for a specific public endpoint.',
        'note': 'Read-only analysis of three fixed public JavaScript bundles. No cookies, user tokens or authentication secrets are requested.'
    })

@app.get('/api/client-config-inspect')
def client_config_inspect():
    """Inspect fixed public API-client source; do not request or expose credentials."""
    path = '/_next/static/chunks/422-5df1ddcb9ef44d09.js'
    try:
        response = SESSION.get(TACH + path, timeout=18)
        if not response.ok:
            return jsonify({'version': VERSION, 'status': response.status_code, 'error': 'JavaScript unavailable'}), 502
        if len(response.content) > 1_000_000:
            return jsonify({'error': 'Unexpected bundle size'}), 502
        source = response.text
        lower = source.lower()

        # Provide only schema excerpts and redacted structural code around the
        # five offsets discovered in the previous diagnostic. Offsets are
        # re-derived from the downloaded bundle rather than blindly trusted.
        needles = {
            'counter_endpoint': 'oneCountersUsercontentApiV1CountersUsercontentUsercontentIdGet',
            'authorization': 'authorization',
            'api_url': 'apiUrl',
            'bearer_schema': 'HTTPBearer',
        }
        offsets = {key: [m.start() for m in re.finditer(re.escape(value.lower()), lower)]
                   for key, value in needles.items()}

        def sanitize(fragment):
            # Remove strings that could contain access credentials, even if
            # bundled in publicly downloadable JavaScript.
            fragment = re.sub(r'(?i)(bearer\s+)[A-Za-z0-9._~+/-]+', r'\1[REDACTED]', fragment)
            fragment = re.sub(r'(?i)((?:access|refresh|id)[_-]?token\s*[:=]\s*[\"\\\'])([^\"\\\']+)', r'\1[REDACTED]', fragment)
            fragment = re.sub(r'(?i)(authorization\s*[:=]\s*[\"\\\'])([^\"\\\']+)', r'\1[REDACTED]', fragment)
            return fragment

        excerpts = []
        for label in ('counter_endpoint', 'authorization', 'api_url'):
            for offset in offsets[label][:8]:
                left = 220 if label == 'authorization' else 280
                right = 380 if label == 'authorization' else 450
                excerpts.append({
                    'kind': label,
                    'offset': offset,
                    'context': sanitize(source[max(0, offset-left):min(len(source), offset+right)])
                })
        return jsonify({
            'version': VERSION,
            'status': response.status_code,
            'bundle_bytes': len(response.content),
            'counter_endpoint_has_explicit_bearer': 'security:["HTTPBearer"]' in source[
                offsets['counter_endpoint'][0]:offsets['counter_endpoint'][0]+250
            ] if offsets['counter_endpoint'] else None,
            'counts': {key: len(value) for key, value in offsets.items()},
            'excerpts': excerpts,
            'note': 'Static public client code only; no credentials, cookies or private endpoints are requested.'
        })
    except requests.RequestException as exc:
        return jsonify({'version': VERSION, 'error': type(exc).__name__}), 502



@app.get('/api/session-source-inspect')
def session_source_inspect():
    """Inspect public bundled source for auth helper and app-version calculation."""
    path = '/_next/static/chunks/422-5df1ddcb9ef44d09.js'
    try:
        response = SESSION.get(TACH + path, timeout=18)
        if not response.ok:
            return jsonify({'version': VERSION, 'status': response.status_code,
                            'error': 'Public JS bundle unavailable'}), 502
        if len(response.content) > 1_000_000:
            return jsonify({'error': 'Unexpected JS bundle size'}), 502
        source = response.text

        def redact(value):
            value = re.sub(r'(?i)(bearer\s+)[A-Za-z0-9._~+/-]{16,}',
                           r'\1[REDACTED]', value)
            value = re.sub(r'(?i)((?:access|refresh|id)[_-]?token\s*[:=]\s*["\\\'])([^"\\\']+)',
                           r'\1[REDACTED]', value)
            return value

        modules = {}
        for module_id, label in (('60051', 'auth_helper'), ('38468', 'app_version')):
            pattern = re.compile(r'(?<!\d)' + re.escape(module_id) + r':\s*\(')
            match = pattern.search(source)
            if not match:
                modules[label] = {'found': False}
                continue
            start = match.start()
            # Minified webpack modules have no reliable line boundaries;
            # provide a bounded excerpt, not guessed module semantics.
            excerpt = source[start:min(len(source), start + 2800)]
            modules[label] = {
                'found': True,
                'offset': start,
                'source_excerpt': redact(excerpt),
                'excerpt_truncated': len(source) > start + 2800
            }

        callsites = []
        for label, pattern in (
            ('auth_helper_call', r'\(0,i\.gf\)\('),
            ('app_version_call', r'\(0,d\.X\)\('),
            ('session_check', r'doesSessionExist'),
            ('guest_detection', r'is_guest|isGuest|guest')
        ):
            matches = list(re.finditer(pattern, source))
            for match in matches[:3]:
                pos = match.start()
                callsites.append({
                    'kind': label,
                    'offset': pos,
                    'context': redact(source[max(0, pos - 160):min(len(source), pos + 240)])
                })

        return jsonify({
            'version': VERSION,
            'status': response.status_code,
            'bundle_bytes': len(response.content),
            'modules': modules,
            'callsites': callsites,
            'note': 'Only public JavaScript source is inspected. No session is created and no token is requested.'
        })
    except requests.RequestException as exc:
        return jsonify({'version': VERSION, 'error': type(exc).__name__}), 502


@app.get('/api/health')
def health():
    return jsonify({'ok': True, 'service': 'tach-creator-studio', 'version': VERSION, 'index_exists': (BASE_DIR / 'index.html').is_file()})


def fetch(path, params=None):
    try:
        response = tach_api_get(path, params)
        response.raise_for_status()
        return response.json(), None
    except requests.HTTPError as exc:
        response = exc.response
        status = response.status_code if response is not None else None
        app.logger.warning('Tach returned HTTP %s for %s', status, path)
        info = classify_error(response) if response is not None else {'tach_status': status}
        return None, {'error': f'Tach вернул HTTP {status}', **info}
    except requests.Timeout:
        return None, {'error': 'Истекло время ожидания Tach'}
    except requests.RequestException as exc:
        app.logger.warning('Tach connection error: %s', type(exc).__name__)
        return None, {'error': 'Ошибка подключения к Tach', 'type': type(exc).__name__}
    except ValueError:
        return None, {'error': 'Tach вернул некорректный JSON или гостевой токен'}


@app.get('/api/guest-auth-check')
def guest_auth_check():
    try:
        token = get_guest_token()
        response = tach_api_get(
            f'/api/v1/counters/usercontent/{SAMPLE_CONTENT_ID}/'
        )
        result = {
            'version': VERSION,
            'guest_token_obtained': bool(token),
            'guest_token_expiry_seconds': max(0, int(GUEST_CACHE['expires_at'] - time.time())),
            'app_version': APP_VERSION,
            'counters_status': response.status_code,
            'counters_content_type': response.headers.get('Content-Type', '').split(';')[0],
        }
        if response.ok:
            try:
                data = response.json()
                result['counters_fields_present'] = [
                    key for key in ('content_viewing', 'content_like_like', 'content_comments')
                    if isinstance(data, dict) and key in data
                ]
            except ValueError:
                result['counters_json_valid'] = False
        else:
            result['counters_error'] = classify_error(response)['tach_message']
        return jsonify(result)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        return jsonify({'version': VERSION, 'guest_token_status': status, 'error': 'Guest token endpoint returned HTTP error'}), 502
    except (requests.RequestException, ValueError) as exc:
        return jsonify({'version': VERSION, 'error': type(exc).__name__, 'stage': 'guest_token'}), 502


@app.get('/api/videos')
def videos():
    nickname = request.args.get('nickname', NICKNAME).strip().lower()
    if nickname != NICKNAME:
        return jsonify({'error': 'Пока поддерживается только профиль vladislove'}), 400
    page_str = request.args.get('page', '1')
    content_type = request.args.get('type', '5')
    if not page_str.isdigit() or not 1 <= int(page_str) <= 100:
        return jsonify({'error': 'Некорректный номер страницы'}), 400
    if content_type not in ('4', '5'):
        return jsonify({'error': 'Допустимые типы: 4 (посты) и 5 (видео)'}), 400
    params = {
        'page_size': 10,
        'order': '-pinned,-published',
        'filter': json.dumps({'user': USER_ID, 'type__in': [int(content_type)], 'markers__in': [2]}, separators=(',', ':')),
        'page': int(page_str),
    }
    data, error = fetch('/api/v1/usercontent/', params)
    if error:
        return jsonify(error), 502
    results = []
    for item in data.get('results', []):
        content = item.get('content') or {}
        body = content.get('text') or ''
        images = content.get('images_ext') or []
        results.append({
            'id': item.get('id'),
            'type': item.get('type'),
            'title': content.get('title') or body[:120] or 'Без названия',
            'text': body,
            'published': item.get('published'),
            'cover': extract_cover(item, content),
            'url': item.get('url'),
        })
    return jsonify({'pagination': data.get('pagination', {}), 'results': results})


@app.get('/api/stats/<item_id>')
def stats(item_id):
    if not re.fullmatch(r'[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}', item_id):
        return jsonify({'error': 'Некорректный ID публикации'}), 400
    data, error = fetch(f'/api/v1/counters/usercontent/{item_id}/')
    if error:
        return jsonify(error), 502
    keys = ('content_viewing', 'content_like_like', 'content_like_dislike', 'content_comments', 'content_hidden')
    return jsonify({key: data.get(key, 0) for key in keys})


@app.get('/api/diagnostics')
def diagnostics():
    # Fixed, read-only checks. No user-provided upstream URLs or credentials.
    checks = [
        ('profile', f'/api/v1/user/{USER_ID}/', None),
        ('posts', '/api/v1/usercontent/', {
            'page_size': 1,
            'page': 1,
            'order': '-pinned,-published',
            'filter': json.dumps({'user': USER_ID, 'type__in': [4], 'markers__in': [2]}, separators=(',', ':')),
        }),
        ('counters', f'/api/v1/counters/usercontent/{SAMPLE_CONTENT_ID}/', None),
    ]
    results = {}
    for name, path, params in checks:
        try:
            response = tach_api_get(path, params)
            entry = {'status': response.status_code, 'content_type': response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()}
            if response.status_code >= 400:
                entry['message'] = classify_error(response)['tach_message']
            else:
                entry['message'] = 'ok'
            results[name] = entry
        except requests.Timeout:
            results[name] = {'status': None, 'message': 'timeout'}
        except requests.RequestException as exc:
            results[name] = {'status': None, 'message': type(exc).__name__}
    return jsonify({'version': VERSION, 'checks': results})



@app.get('/api/auth-module-locate')
def auth_module_locate():
    """Locate public webpack module 30509; no session or tokens requested."""
    try:
        batch = int(request.args.get('batch', '1'))
    except ValueError:
        return jsonify({'error': 'batch must be an integer 1 through 5'}), 400
    if batch not in range(1, 6):
        return jsonify({'error': 'batch must be 1 through 5'}), 400

    try:
        page = SESSION.get(TACH + PUBLIC_POST_PATH, timeout=12,
                           headers={'Accept': 'text/html'})
        page.raise_for_status()
        inventory = ScriptInventoryParser()
        inventory.feed(page.text)
        files = inventory.scripts
        selected = files[(batch - 1) * 8:batch * 8]
        matches = []
        checked = []
        for src in selected:
            entry = {'file': src}
            try:
                response = SESSION.get(TACH + src, timeout=9)
                entry['status'] = response.status_code
                if response.ok and len(response.content) <= 2_000_000:
                    source = response.text
                    # Find the definition, not merely a reference r(30509).
                    module = re.search(r'(?<!\d)30509\s*:\s*\(', source)
                    entry['module_found'] = bool(module)
                    if module:
                        pos = module.start()
                        # Static public source only. Limit excerpt to 5000 chars.
                        excerpt = source[pos:pos + 5000]
                        excerpt = re.sub(
                            r'(?i)(bearer\s+)[A-Za-z0-9._~+/-]{16,}',
                            r'\1[REDACTED]', excerpt
                        )
                        matches.append({
                            'file': src,
                            'offset': pos,
                            'module_excerpt': excerpt,
                            'excerpt_truncated': len(source) > pos + 5000,
                        })
            except requests.RequestException as exc:
                entry['error'] = type(exc).__name__
            checked.append(entry)
        return jsonify({
            'version': VERSION,
            'batch': batch,
            'total_files': len(files),
            'checked': checked,
            'matches': matches,
            'next_batch': batch + 1 if batch < 5 and not matches else None,
            'note': 'Read-only public JS inspection. No credentials requested.'
        })
    except requests.RequestException as exc:
        return jsonify({'version': VERSION, 'error': type(exc).__name__}), 502



# Creator Studio 3.0: dated snapshots. Set DATABASE_PATH to a persistent Render disk.
DATABASE_PATH = os.environ.get('DATABASE_PATH', str(BASE_DIR / 'tach_analytics.sqlite3'))
ANALYTICS_TZ = os.environ.get('ANALYTICS_TZ', 'Europe/Moscow')
ANALYTICS_LOCK = threading.RLock()
# Each request carries its own validated public Tach profile; no global mutable identity.
ACTIVE_PROFILE = contextvars.ContextVar('tach_active_profile', default=(NICKNAME, USER_ID))
PROFILE_LOOKUP_CACHE = {}
PROFILE_LOOKUP_LOCK = threading.RLock()
PROFILE_STATES = {}
PROFILE_STATES_LOCK = threading.RLock()
PROFILE_REFRESH_LOCKS = {}

def profile_lock():
    key = current_profile()[1]
    with PROFILE_STATES_LOCK:
        return PROFILE_REFRESH_LOCKS.setdefault(key, threading.RLock())
UUID_RE = re.compile(r'^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$', re.I)
NICK_RE = re.compile(r'^[A-Za-z0-9_.-]{2,50}$')


def current_profile():
    return ACTIVE_PROFILE.get()


def profile_refresh_state():
    key = current_profile()[1]
    with PROFILE_STATES_LOCK:
        return PROFILE_STATES.setdefault(key, {'at': 0, 'error': None})


def extract_nickname(value):
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
        value = parts[1] if parts[0] in ('p', 'user', 'profile') and len(parts) > 1 else parts[0]
    value = value.lstrip('@').strip()
    if not NICK_RE.fullmatch(value):
        raise ValueError('Укажи ник Tach: латинские буквы, цифры, точка, _ или -')
    return value.lower()


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


def resolve_public_profile(raw):
    nick = extract_nickname(raw)
    if nick == NICKNAME:
        return (NICKNAME, USER_ID)
    with PROFILE_LOOKUP_LOCK:
        cached = PROFILE_LOOKUP_CACHE.get(nick)
        if cached and cached[0] > time.time():
            return (nick, cached[1])
    # Fetch only a fixed trusted Tach origin; never fetch user-supplied URLs.
    html = ''
    for profile_path in ('/p/' + nick, '/@' + nick):
        resp = SESSION.get(TACH + profile_path, timeout=15, headers={'Accept':'text/html'})
        if resp.status_code == 404:
            continue
        resp.raise_for_status()
        if 'text/html' in resp.headers.get('Content-Type', ''):
            html = resp.text[:2000000]
            if nick in html.lower():
                break
    if not html:
        raise LookupError('Публичная страница профиля Tach не найдена')
    # Public Next.js page embeds a user object. Find UUID near the nickname,
    # then confirm the candidate against Tach's public user endpoint.
    candidates = []
    escaped = html.replace('\\"', '"').replace('\"', '"')
    for match in re.finditer(re.escape(nick), escaped, flags=re.I):
        context = escaped[max(0,match.start()-2200):match.end()+2200]
        for m in re.finditer(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', context, re.I):
            uuid = m.group().lower()
            if uuid not in candidates:
                candidates.append(uuid)
            if len(candidates) >= 30:
                break
        if len(candidates) >= 30:
            break
    for uid in candidates:
        data, error = fetch('/api/v1/user/' + uid + '/')
        if error or not isinstance(data, dict):
            continue
        if _profile_name(data) == nick:
            with PROFILE_LOOKUP_LOCK:
                PROFILE_LOOKUP_CACHE[nick] = (time.time()+3600, uid)
            return (nick, uid)
    raise LookupError('Не удалось подтвердить профиль по публичным данным Tach. Попробуй ссылку на профиль.')


@app.before_request
def select_analytics_profile():
    if not request.path.startswith('/api/analytics'):
        return None
    if request.path.endswith('/source-check'):
        return None
    # Scheduler defaults to the original profile; public clients choose explicitly.
    raw = NICKNAME if request.path.endswith('/cron') else request.args.get('profile', NICKNAME)
    try:
        ACTIVE_PROFILE.set(resolve_public_profile(raw))
    except ValueError as exc:
        return jsonify({'error':str(exc)}),400
    except LookupError as exc:
        return jsonify({'error':str(exc)}),404
    except requests.RequestException:
        return jsonify({'error':'Не удалось проверить профиль Tach. Повтори позже.'}),502


@app.get('/api/profiles/resolve')
def profile_resolve_route():
    try:
        nick, uid = resolve_public_profile(request.args.get('profile', NICKNAME))
        return jsonify({'nickname':nick, 'user_id':uid, 'verified':True})
    except ValueError as exc:
        return jsonify({'error':str(exc)}),400
    except LookupError as exc:
        return jsonify({'error':str(exc)}),404
    except requests.RequestException:
        return jsonify({'error':'Tach сейчас не отвечает'}),502


REFRESH_SECONDS = max(60, int(os.environ.get('REFRESH_SECONDS', '900')))
LAST_REFRESH = {'at': 0, 'error': None}


def analytics_now():
    try:
        return datetime.now(ZoneInfo(ANALYTICS_TZ))
    except Exception:
        return datetime.now(timezone.utc)


def analytics_db():
    nick, uid = current_profile()
    db_path = DATABASE_PATH if uid == USER_ID else str(Path(DATABASE_PATH).with_name('tach_profile_' + hashlib.sha256(uid.encode()).hexdigest()[:24] + '.sqlite3'))
    parent = Path(db_path).parent
    parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=20000')
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS content (
      id TEXT PRIMARY KEY, title TEXT NOT NULL, published TEXT,
      type TEXT, url TEXT, cover TEXT, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS snapshots (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      content_id TEXT NOT NULL,
      observed_at TEXT NOT NULL,
      observed_day TEXT NOT NULL,
      views INTEGER NOT NULL,
      likes INTEGER NOT NULL,
      comments INTEGER NOT NULL,
      shares INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS snapshots_lookup ON snapshots(content_id, observed_at);
    CREATE INDEX IF NOT EXISTS snapshots_day ON snapshots(observed_day);
    CREATE TABLE IF NOT EXISTS profile_snapshots (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      observed_at TEXT NOT NULL, observed_day TEXT NOT NULL,
      followers INTEGER
    );
    ''')
    return conn


def as_count(value):
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def fetch_content_page(page, types):
    params = {
        'page_size': 30, 'page': page, 'order': '-pinned,-published',
        'filter': json.dumps({'user': current_profile()[1], 'type__in': types, 'markers__in': [2]}, separators=(',', ':')),
    }
    data, error = fetch('/api/v1/usercontent/', params)
    if error:
        raise RuntimeError(error['error'])
    if not isinstance(data, dict):
        raise RuntimeError('Некорректный ответ списка публикаций')
    return data


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


def collect_analytics():
    now = analytics_now()
    stamp = now.isoformat(timespec='seconds')
    day = now.date().isoformat()
    found = {}
    list_errors = []
    # Separate queries are compatible with the known Tach content filters.
    for content_type in (4, 5):
        for page in range(1, 11):
            try:
                data = fetch_content_page(page, [content_type])
            except Exception as exc:
                list_errors.append('Тип %s, стр. %s: %s' % (content_type, page, str(exc)[:120]))
                break
            items = data.get('results') or []
            if not isinstance(items, list):
                break
            for item in items:
                if not isinstance(item, dict) or not item.get('id'):
                    continue
                c = item.get('content') or {}
                if not isinstance(c, dict):
                    c = {}
                images = c.get('images_ext') or []
                cover = extract_cover(item, c)
                body = c.get('text') or ''
                found[str(item['id'])] = {
                    'id': str(item['id']), 'title': str(c.get('title') or body[:110] or 'Публикация'),
                    'published': item.get('published'), 'type': str(item.get('type') or content_type),
                    'url': item.get('url'), 'cover': cover,
                }
            if len(items) < 30:
                break
    if not found and list_errors:
        raise RuntimeError('; '.join(list_errors[:2]))
    # Always retain earlier publications in DB, even if the current page list is incomplete.
    results = []
    with analytics_db() as conn:
        for item in found.values():
            conn.execute('''INSERT INTO content(id,title,published,type,url,cover,updated_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,published=excluded.published,type=excluded.type,
                url=excluded.url,cover=excluded.cover,updated_at=excluded.updated_at''',
                (item['id'],item['title'],item['published'],item['type'],item['url'],item['cover'],stamp))
        ids = [r['id'] for r in conn.execute('SELECT id FROM content').fetchall()]
        for content_id in ids:
            stats, error = fetch('/api/v1/counters/usercontent/%s/' % content_id)
            if error or not isinstance(stats, dict):
                continue
            values = (as_count(stats.get('content_viewing')),
                      as_count(stats.get('content_like_like')),
                      as_count(stats.get('content_comments')),
                      as_count(stats.get('content_reposts') or stats.get('content_shares')))
            conn.execute('''INSERT INTO snapshots
                (content_id, observed_at, observed_day, views, likes, comments, shares)
                VALUES(?,?,?,?,?,?,?)''', (content_id, stamp, day, *values))
            results.append(content_id)
        profile, error = fetch('/api/v1/counters/user/%s/' % current_profile()[1])
        if not error and isinstance(profile, dict):
            follower_keys = ('user_subscribers', 'subscribers', 'followers', 'followers_count', 'user_followers')
            followers = next((as_count(profile[k]) for k in follower_keys if k in profile), None)
            if followers is not None:
                conn.execute('INSERT INTO profile_snapshots(observed_at,observed_day,followers) VALUES(?,?,?)',
                             (stamp, day, followers))
    if not results:
        raise RuntimeError('Список публикаций найден, но API счётчиков не вернул статистику')
    return {'collected': len(results), 'discovered': len(found), 'warnings': list_errors[:3], 'at': stamp}


def refresh_analytics(force=False):
    with profile_lock():
        now = time.time()
        if not force and now - profile_refresh_state()['at'] < REFRESH_SECONDS:
            return None
        # Prevent repeated upstream requests on every page load after a failure.
        profile_refresh_state()['at'] = now
        try:
            result = collect_analytics()
            profile_refresh_state()['error'] = None
            return result
        except Exception as exc:
            profile_refresh_state()['error'] = str(exc)[:300]
            app.logger.warning('Analytics refresh: %s', str(exc)[:300])
            return None


def read_dashboard(days):
    with analytics_db() as conn:
        content_rows = conn.execute('SELECT * FROM content ORDER BY published DESC').fetchall()
        latest = conn.execute('''SELECT s.* FROM snapshots s
            JOIN (SELECT content_id, MAX(id) AS max_id FROM snapshots GROUP BY content_id) z
            ON s.id=z.max_id''').fetchall()
        latest_by_id = {r['content_id']: dict(r) for r in latest}
        items = []
        for row in content_rows:
            item = dict(row)
            stat = latest_by_id.get(item['id']) or {}
            item.update({key: stat.get(key, 0) for key in ('views','likes','comments','shares')})
            item['observed_at'] = stat.get('observed_at')
            items.append(item)
        today = analytics_now().date()
        start = (today - timedelta(days=days-1)).isoformat()
        # One snapshot per publication per day: last observation on that day.
        rows = conn.execute('''SELECT s.content_id,s.observed_day,s.views,s.likes,s.comments
            FROM snapshots s JOIN (
              SELECT content_id,observed_day,MAX(id) AS max_id FROM snapshots
              WHERE observed_day >= ? GROUP BY content_id,observed_day
            ) z ON s.id=z.max_id ORDER BY s.observed_day''', (start,)).fetchall()
        grouped = {}
        for r in rows:
            key = r['observed_day']
            g = grouped.setdefault(key, {'views':0,'likes':0,'comments':0,'observed_posts':0})
            for metric in ('views','likes','comments'):
                g[metric] += r[metric]
            g['observed_posts'] += 1
        history = [{'date': d, **v} for d,v in sorted(grouped.items())]
        followers_row = conn.execute('SELECT followers FROM profile_snapshots ORDER BY id DESC LIMIT 1').fetchone()
        followers_history = [dict(r) for r in conn.execute('''SELECT p.observed_day AS date,p.followers FROM profile_snapshots p
            JOIN (SELECT observed_day,MAX(id) max_id FROM profile_snapshots
            WHERE observed_day >= ? GROUP BY observed_day) z ON p.id=z.max_id
            ORDER BY p.observed_day''', (start,)).fetchall()]
        observed = conn.execute('SELECT MIN(observed_at) first, MAX(observed_at) last FROM snapshots').fetchone()
    totals = {key:sum(x[key] for x in items) for key in ('views','likes','comments','shares')}
    # Aggregate observed changes into hourly buckets. These are measurement intervals,
    # not the exact timestamps of individual views.
    hourly = []
    with analytics_db() as conn:
        cutoff = (analytics_now() - timedelta(days=min(days, 30))).isoformat(timespec='seconds')
        samples = conn.execute("""SELECT content_id, observed_at, views FROM snapshots
            WHERE observed_at >= ? ORDER BY content_id, observed_at, id""", (cutoff,)).fetchall()
    previous = {}
    buckets = {}
    for sample in samples:
        cid = sample['content_id']
        current = int(sample['views'])
        old = previous.get(cid)
        if old is not None:
            delta = max(0, current - old)
            hour = sample['observed_at'][:13] + ':00'
            buckets[hour] = buckets.get(hour, 0) + delta
        previous[cid] = current
    hourly = [{'date': k, 'views': v} for k, v in sorted(buckets.items())]
    return {'version':VERSION,'profile':current_profile()[0],'days':days,'totals':totals,
            'followers':followers_row['followers'] if followers_row else None,
            'items':items,'history':history,'hourly_growth':hourly,'followers_history':followers_history,
            'first_observed_at':observed['first'],'last_observed_at':observed['last'],
            'refresh_error':profile_refresh_state()['error'], 'timezone':ANALYTICS_TZ,
            'storage': 'sqlite', 'refresh_interval_seconds':REFRESH_SECONDS}


@app.get('/api/analytics')
def analytics():
    try:
        days = int(request.args.get('days','30'))
    except ValueError:
        return jsonify({'error':'Неверный период'}),400
    if days not in (7,30,90,365):
        return jsonify({'error':'Доступные периоды: 7, 30, 90, 365 дней'}),400
    try:
        refresh_analytics()
        return jsonify(read_dashboard(days))
    except (sqlite3.Error, OSError) as exc:
        return jsonify({'error':'Не удалось открыть базу аналитики','detail':str(exc)[:160]}),500


@app.post('/api/analytics/refresh')
def analytics_refresh():
    # Rate limited: don't expose unlimited forced requests to Tach.
    if time.time() - profile_refresh_state()['at'] < 60:
        return jsonify({'error':'Повторите обновление через минуту'}),429
    result = refresh_analytics(force=True)
    return jsonify({'ok':result is not None,'result':result,'error':profile_refresh_state()['error']}), 200 if result else 502


@app.get('/api/analytics/source-check')
def analytics_source_check():
    """Report the public schema for Tach's date-based counters endpoint.
    A system-api endpoint is not called: availability/permission are unverified.
    """
    bundle = '/_next/static/chunks/422-5df1ddcb9ef44d09.js'
    try:
        response = SESSION.get(TACH + bundle, timeout=15)
        response.raise_for_status()
        source = response.text
        paths = [
            '/system-api/v1/counters-data/by-usercontent-date/',
            '/system-api/v1/counters-data/by-usercontent/',
        ]
        matches = []
        for path in paths:
            at = source.find(path)
            matches.append({'path': path, 'found_in_public_js': at >= 0,
                            'schema_excerpt': source[max(0,at-110):at+len(path)+130] if at >= 0 else None})
        return jsonify({'version':VERSION,'endpoints':matches,
                        'access_verified':False,
                        'note':'System API routes appear in a public JS schema, but are not necessarily accessible to guest sessions. No requests were made to these routes.'})
    except requests.RequestException as exc:
        return jsonify({'error':type(exc).__name__}),502


@app.get('/api/analytics/hourly')
def analytics_hourly():
    try:
        refresh_analytics()
        data = read_dashboard(7)
        return jsonify({'version':VERSION,'timezone':ANALYTICS_TZ,
                        'points':data['hourly_growth'],
                        'meaning':'Views first detected in the interval ending at the observation time; not exact view timestamps.'})
    except (sqlite3.Error, OSError) as exc:
        return jsonify({'error':str(exc)[:160]}),500


@app.get('/api/analytics/cron')
def analytics_cron():
    """Optional external scheduler: configure CRON_SECRET on Render and send Authorization: Bearer <secret>."""
    import hmac
    secret = os.environ.get('CRON_SECRET', '')
    supplied = request.headers.get('Authorization', '')
    if not secret or not hmac.compare_digest(supplied, 'Bearer ' + secret):
        return jsonify({'error':'Unauthorized'}),401
    result = refresh_analytics()
    return jsonify({'ok':profile_refresh_state()['error'] is None,
                    'refreshed':result is not None,'result':result,
                    'error':profile_refresh_state()['error']}), 200 if profile_refresh_state()['error'] is None else 502


@app.get('/api/analytics/export.csv')
def analytics_export():
    import csv
    import io
    from flask import Response
    with analytics_db() as conn:
        rows = conn.execute('''SELECT s.observed_at,s.observed_day,c.title,c.id,s.views,s.likes,s.comments,s.shares
            FROM snapshots s JOIN content c ON c.id=s.content_id
            ORDER BY s.observed_at DESC''').fetchall()
    output=io.StringIO()
    writer=csv.writer(output)
    writer.writerow(['Дата и время','Дата','Публикация','ID','Просмотры','Лайки','Комментарии','Репосты'])
    for row in rows:
        writer.writerow(list(row))
    return Response('\ufeff'+output.getvalue(),mimetype='text/csv',headers={
        'Content-Disposition':'attachment; filename=tach_analytics.csv'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '10000')))
