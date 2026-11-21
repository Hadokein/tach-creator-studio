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
SESSION = requests.Session()
SESSION.headers.update({'Accept': 'application/json', 'User-Agent': 'TachCreatorStudio/1.2'})


@app.get('/')
def home():
    if not (BASE_DIR / 'index.html').is_file():
        return jsonify({'error': 'index.html не найден рядом с app.py'}), 404
    return send_from_directory(str(BASE_DIR), 'index.html')


@app.get('/api/health')
def health():
    return jsonify({'ok': True, 'service': 'tach-creator-studio', 'version': '1.2', 'index_exists': (BASE_DIR / 'index.html').is_file()})


def fetch(path, params=None):
    try:
        response = SESSION.get(TACH + path, params=params, timeout=15)
        response.raise_for_status()
        return response.json(), None
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        app.logger.warning('Tach returned HTTP %s for %s', status, path)
        return None, {'error': f'Tach вернул HTTP {status}', 'tach_status': status}
    except requests.Timeout:
        return None, {'error': 'Истекло время ожидания Tach'}
    except requests.RequestException as exc:
        app.logger.warning('Tach connection error: %s', type(exc).__name__)
        return None, {'error': 'Ошибка подключения к Tach', 'type': type(exc).__name__}
    except ValueError:
        return None, {'error': 'Tach вернул не JSON'}


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
            'cover': content.get('cover_url') or (images[0].get('url') if images else None),
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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '10000')))
