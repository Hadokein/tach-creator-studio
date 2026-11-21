import json
import os
import re
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__)
BASE_DIR = Path(__file__).resolve().parent
TACH = 'https://tach.id'
USER_ID = '7fd088f2-79a4-4ec4-a2a1-f1b45724fb99'
NICKNAME = 'vladislove'
SAMPLE_CONTENT_ID = '1a7e555c-5d34-444d-a825-7e1d4e4e3152'
SESSION = requests.Session()
SESSION.headers.update({'Accept': 'application/json', 'User-Agent': 'TachCreatorStudio/1.4'})


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


@app.get('/api/health')
def health():
    return jsonify({'ok': True, 'service': 'tach-creator-studio', 'version': '1.4', 'index_exists': (BASE_DIR / 'index.html').is_file()})


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
    return jsonify({'version': '1.4', 'checks': results})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '10000')))
