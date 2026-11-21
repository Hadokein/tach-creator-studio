
import os
import re
import json
import logging

from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory
import requests

# ==========================================
# НАСТРОЙКИ
# ==========================================

BASE_DIR = Path(__file__).resolve().parent

app = Flask(__name__)

TACH = "https://tach.id"

USER_ID = "7fd088f2-79a4-4ec4-a2a1-f1b45724fb99"
NICKNAME = "vladislove"

SESSION = requests.Session()

SESSION.headers.update({
    "Accept": "application/json",
    "User-Agent": "TachCreatorStudio/1.0"
})

logging.basicConfig(level=logging.INFO)


# ==========================================
# ГЛАВНАЯ СТРАНИЦА
# ==========================================

@app.get("/")
def home():
    index_file = BASE_DIR / "index.html"

    if not index_file.is_file():
        return jsonify({
            "error": "Файл index.html не найден",
            "expected": "index.html рядом с app.py"
        }), 404

    return send_from_directory(
        str(BASE_DIR),
        "index.html"
    )


# ==========================================
# ПРОВЕРКА СЕРВЕРА
# ==========================================

@app.get("/api/health")
def health():
    return jsonify({
        "ok": True,
        "service": "tach-creator-studio",
        "version": "1.1",
        "index_exists": (BASE_DIR / "index.html").is_file()
    })


# ==========================================
# ЗАПРОСЫ К TACH
# ==========================================

def fetch(path, params=None):
    try:
        response = SESSION.get(
            TACH + path,
            params=params,
            timeout=15
        )

        response.raise_for_status()

        return response.json(), None

    except requests.HTTPError as exc:
        status = exc.response.status_code

        app.logger.warning(
            "Tach API HTTP %s: %s",
            status,
            path
        )

        return None, {
            "error": f"Tach вернул HTTP {status}",
            "tach_status": status
        }

    except requests.Timeout:
        return None, {
            "error": "Превышено время ожидания Tach"
        }

    except requests.ConnectionError:
        return None, {
            "error": "Не удалось подключиться к Tach"
        }

    except requests.RequestException as exc:
        app.logger.warning(
            "Ошибка запроса Tach: %s",
            type(exc).__name__
        )

        return None, {
            "error": "Ошибка подключения к Tach",
            "type": type(exc).__name__
        }

    except ValueError:
        return None, {
            "error": "Tach вернул ответ не в формате JSON"
        }


# ==========================================
# ПУБЛИКАЦИИ АВТОРА
# ==========================================

@app.get("/api/videos")
def videos():

    nickname = request.args.get(
        "nickname",
        NICKNAME
    ).strip().lower()

    if nickname != NICKNAME:
        return jsonify({
            "error": (
                "Пока поддерживается только "
                "профиль vladislove"
            )
        }), 400

    page = request.args.get("page", "1")

    if not page.isdigit():
        return jsonify({
            "error": "Некорректный номер страницы"
        }), 400

    page = int(page)

    if page < 1 or page > 100:
        return jsonify({
            "error": "Страница должна быть от 1 до 100"
        }), 400

    filters = {
        "user": USER_ID,
        "type__in": [5],
        "markers__in": [2]
    }

    params = {
        "page_size": 10,
        "page": page,
        "order": "-pinned,-published",
        "filter": json.dumps(
            filters,
            separators=(",", ":")
        )
    }

    data, error = fetch(
        "/api/v1/usercontent/",
        params
    )

    if error:
        return jsonify(error), 502

    result = []

    for item in data.get("results", []):

        content = item.get("content") or {}

        result.append({
            "id": item.get("id"),
            "title": content.get("title")
                     or "Без названия",
            "description": content.get(
                "description"
            ),
            "published": item.get("published"),
            "cover": content.get("cover_url"),
            "duration": content.get("duration"),
            "url": item.get("url")
        })

    return jsonify({
        "pagination": data.get("pagination", {}),
        "results": result
    })


# ==========================================
# СТАТИСТИКА ПУБЛИКАЦИИ
# ==========================================

@app.get("/api/stats/<item_id>")
def stats(item_id):

    uuid_pattern = (
        r"[a-fA-F0-9]{8}-"
        r"[a-fA-F0-9]{4}-"
        r"[a-fA-F0-9]{4}-"
        r"[a-fA-F0-9]{4}-"
        r"[a-fA-F0-9]{12}"
    )

    if not re.fullmatch(uuid_pattern, item_id):
        return jsonify({
            "error": "Некорректный ID публикации"
        }), 400

    data, error = fetch(
        f"/api/v1/counters/usercontent/{item_id}/"
    )

    if error:
        return jsonify(error), 502

    return jsonify({
        "content_viewing": data.get(
            "content_viewing", 0
        ),
        "content_like_like": data.get(
            "content_like_like", 0
        ),
        "content_like_dislike": data.get(
            "content_like_dislike", 0
        ),
        "content_comments": data.get(
            "content_comments", 0
        ),
        "content_hidden": data.get(
            "content_hidden", 0
        )
    })


# ==========================================
# ЗАПУСК
# ==========================================

if __name__ == "__main__":

    port = int(
        os.environ.get("PORT", "10000")
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
