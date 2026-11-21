import json
import base64
import time
import threading
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
VERSION = '2.3'
SESSION = requests.Session()
SESSION.headers.update({'Accept': 'application/json', 'User-Agent': 'TachCreatorStudio/2.3'})
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
        return jsonify({'error': 'index.html Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½ ÑÑÐ´Ð¾Ð¼ Ñ app.py'}), 404
    return send_from_directory(str(BASE_DIR), 'index.html')


@app.get('/tach_browser_test.html')
def browser_test():
    if not (BASE_DIR / 'tach_browser_test.html').is_file():
        return jsonify({'error': 'tach_browser_test.html Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½ ÑÑÐ´Ð¾Ð¼ Ñ app.py'}), 404
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
        return None, {'error': f'Tach Ð²ÐµÑÐ½ÑÐ» HTTP {status}', **info}
    except requests.Timeout:
        return None, {'error': 'ÐÑÑÐµÐºÐ»Ð¾ Ð²ÑÐµÐ¼Ñ Ð¾Ð¶Ð¸Ð´Ð°Ð½Ð¸Ñ Tach'}
    except requests.RequestException as exc:
        app.logger.warning('Tach connection error: %s', type(exc).__name__)
        return None, {'error': 'ÐÑÐ¸Ð±ÐºÐ° Ð¿Ð¾Ð´ÐºÐ»ÑÑÐµÐ½Ð¸Ñ Ðº Tach', 'type': type(exc).__name__}
    except ValueError:
        return None, {'error': 'Tach Ð²ÐµÑÐ½ÑÐ» Ð½ÐµÐºÐ¾ÑÑÐµÐºÑÐ½ÑÐ¹ JSON Ð¸Ð»Ð¸ Ð³Ð¾ÑÑÐµÐ²Ð¾Ð¹ ÑÐ¾ÐºÐµÐ½'}


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
        return jsonify({'error': 'ÐÐ¾ÐºÐ° Ð¿Ð¾Ð´Ð´ÐµÑÐ¶Ð¸Ð²Ð°ÐµÑÑÑ ÑÐ¾Ð»ÑÐºÐ¾ Ð¿ÑÐ¾ÑÐ¸Ð»Ñ vladislove'}), 400
    page_str = request.args.get('page', '1')
    content_type = request.args.get('type', '5')
    if not page_str.isdigit() or not 1 <= int(page_str) <= 100:
        return jsonify({'error': 'ÐÐµÐºÐ¾ÑÑÐµÐºÑÐ½ÑÐ¹ Ð½Ð¾Ð¼ÐµÑ ÑÑÑÐ°Ð½Ð¸ÑÑ'}), 400
    if content_type not in ('4', '5'):
        return jsonify({'error': 'ÐÐ¾Ð¿ÑÑÑÐ¸Ð¼ÑÐµ ÑÐ¸Ð¿Ñ: 4 (Ð¿Ð¾ÑÑÑ) Ð¸ 5 (Ð²Ð¸Ð´ÐµÐ¾)'}), 400
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
            'title': content.get('title') or body[:120] or 'ÐÐµÐ· Ð½Ð°Ð·Ð²Ð°Ð½Ð¸Ñ',
            'text': body,
            'published': item.get('published'),
            'cover': content.get('cover_url') or (images[0].get('url') if images and isinstance(images[0], dict) else None),
            'url': item.get('url'),
        })
    return jsonify({'pagination': data.get('pagination', {}), 'results': results})


@app.get('/api/stats/<item_id>')
def stats(item_id):
    if not re.fullmatch(r'[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}', item_id):
        return jsonify({'error': 'ÐÐµÐºÐ¾ÑÑÐµÐºÑÐ½ÑÐ¹ ID Ð¿ÑÐ±Ð»Ð¸ÐºÐ°ÑÐ¸Ð¸'}), 400
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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '10000')))
