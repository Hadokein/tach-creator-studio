import json
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
VERSION = '1.7'
SESSION = requests.Session()
SESSION.headers.update({'Accept': 'application/json', 'User-Agent': 'TachCreatorStudio/1.7'})


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


@app.get('/api/health')
def health():
    return jsonify({'ok': True, 'service': 'tach-creator-studio', 'version': VERSION, 'index_exists': (BASE_DIR / 'index.html').is_file()})


def fetch(path, params=None):
    try:
        response = SESSION.get(TACH + path, params=params, timeout=15)
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
        return None, {'error': 'Tach Ð²ÐµÑÐ½ÑÐ» Ð½Ðµ JSON'}


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
            response = SESSION.get(TACH + path, params=params, timeout=10)
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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '10000')))
