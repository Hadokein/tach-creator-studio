import os
import re
from urllib.parse import urlparse
from flask import Flask, jsonify, request, send_from_directory
import requests

app = Flask(__name__)
TACH = 'https://tach.id'
SESSION = requests.Session()
SESSION.headers.update({'Accept':'application/json','User-Agent':'TachCreatorStudio/0.1 (+public stats)'})
USER_ID = '7fd088f2-79a4-4ec4-a2a1-f1b45724fb99'

@app.get('/')
def home():
    return send_from_directory('.', 'index.html')

@app.get('/api/health')
def health():
    return jsonify({'ok': True, 'service':'tach-creator-studio'})

def fetch(path, params=None):
    try:
        response = SESSION.get(TACH + path, params=params, timeout=12)
        response.raise_for_status()
        return response.json(), None
    except requests.RequestException as exc:
        return None, f'Tach недоступен для сервера ({type(exc).__name__}). Возможна защита от внешних запросов.'
    except ValueError:
        return None, 'Tach вернул ответ не в формате JSON.'

@app.get('/api/videos')
def videos():
    nickname = request.args.get('nickname','vladislove').strip().lower()
    if nickname != 'vladislove':
        return jsonify({'error':'Пока поддерживается только проверенный профиль vladislove. Для остальных нужен безопасный поиск ID автора.'}), 400
    page = request.args.get('page','1')
    if not page.isdigit() or not 1 <= int(page) <= 100:
        return jsonify({'error':'Некорректная страница'}), 400
    params = {'page_size':10,'page':int(page),'order':'-pinned,-published','filter':'{"user":"'+USER_ID+'","type__in":[5],"markers__in":[2]}'}
    data, error = fetch('/api/v1/usercontent/', params)
    if error:
        return jsonify({'error':error}), 502
    result = []
    for item in data.get('results',[]):
        content = item.get('content') or {}
        result.append({'id':item.get('id'),'title':content.get('title') or 'Без названия','published':item.get('published'),'cover':content.get('cover_url'),'url':item.get('url')})
    return jsonify({'pagination':data.get('pagination',{}),'results':result})

@app.get('/api/stats/<item_id>')
def stats(item_id):
    if not re.fullmatch(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}',item_id):
        return jsonify({'error':'Некорректный ID'}),400
    data,error=fetch(f'/api/v1/counters/usercontent/{item_id}/')
    if error:
        return jsonify({'error':error}),502
    return jsonify({key:data.get(key,0) for key in ('content_viewing','content_like_like','content_like_dislike','content_comments','content_hidden')})

if __name__ == '__main__':
    app.run(host='0.0.0.0',port=int(os.environ.get('PORT','10000')))
