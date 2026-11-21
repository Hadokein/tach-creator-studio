
import json
import os
import re
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
TACH = "https://tach.id"

USER_ID = "7fd088f2-79a4-4ec4-a2a1-f1b45724fb99"
NICKNAME = "vladislove"
VERSION = "1.5"

SESSION = requests.Session()
SESSION.headers.update({
    "Accept": "application/json",
    "User-Agent": "TachCreatorStudio/1.5"
})


# Главная страница приложения
@app.get("/")
def home():
    if not (BASE_DIR / "index.html").is_file():
        return jsonify({
            "error": "index.html не найден рядом с app.py"
        }), 404

    return send_from_directory(str(BASE_DIR), "index.html")


# Тестовая HTML-страница
@app.get("/tach_browser_test.html")
def browser_test():
    if not (BASE_DIR / "tach_browser_test.html").is_file():
        return jsonify({
            "error": "tach_browser_test.html не найден рядом с app.py"
        }), 404

    return send_from_directory(
        str(BASE_DIR),
        "tach_browser_test.html"
    )


# Проверка работы приложения
@app.get("/api/health")
def health():
    return jsonify({
        "ok": True,
        "service": "tach-creator-studio",
        "version": VERSION,
        "index_exists": (BASE_DIR / "index.html").is_file(),
        "browser_test_exists": (
            BASE_DIR / "tach_browser_test.html"
        ).is_file()
    })


# Запрос к Tach API
def fetch(path, params=None):
    try:
        response = SESSION.get(
            TACH + path,
            params=params,
            timeout=15
        )

        if not response.ok:
            message = response.text[:300]

            app.logger.warning(
                "Tach HTTP %s for %s",
                response.status_code,
                path
            )

            return None, {
                "error": f"Tach вернул HTTP {response.status_code}",
                "tach_status": response.status_code,
                "tach_content_type": response.headers.get(
                    "Content-Type", ""
                ),
                "tach_message": message
            }

        try:
            return response.json(), None
        except ValueError:
            return None, {
                "error": "Tach вернул не JSON",
                "tach_content_type": response.headers.get(
                    "Content-Type", ""
                )
            }

    except requests.Timeout:
        return None, {
            "error": "Истекло время ожидания Tach"
        }

    except requests.RequestException as exc:
        app.logger.warning(
            "Tach connection error: %s",
            type(exc).__name__
        )

        return None, {
            "error": "Ошибка подключения к Tach",
            "type": type(exc).__name__
        }


# Список публикаций и видео
@app.get("/api/videos")
def videos():
    nickname = request.args.get(
        "nickname", NICKNAME
    ).strip().lower()

    if nickname != NICKNAME:
        return jsonify({
            "error": "Пока поддерживается только профиль vladislove"
        }), 400

    page_str = request.args.get("page", "1")
    content_type = request.args.get("type", "5")

    if not page_str.isdigit() or not 1 <= int(page_str) <= 100:
        return jsonify({
            "error": "Некорректный номер страницы"
        }), 400

    if content_type not in ("4", "5"):
        return jsonify({
            "error": "Допустимые типы: 4 (посты) и 5 (видео)"
        }), 400

    params = {
        "page_size": 10,
        "order": "-pinned,-published",
        "filter": json.dumps({
            "user": USER_ID,
            "type__in": [int(content_type)],
            "markers__in": [2]
        }, separators=(",", ":")),
        "page": int(page_str)
    }

    data, error = fetch(
        "/api/v1/usercontent/",
        params
    )

    if error:
        return jsonify(error), 502

    results = []

    for item in data.get("results", []):
        content = item.get("content") or {}
        body = content.get("text") or ""
        images = content.get("images_ext") or []

        cover = content.get("cover_url")

        if not cover and images:
            first_image = images[0]
            if isinstance(first_image, dict):
                cover = first_image.get("url")

        results.append({
            "id": item.get("id"),
            "type": item.get("type"),
            "title": (
                content.get("title")
                or body[:120]
                or "Без названия"
            ),
            "text": body,
            "published": item.get("published"),
            "cover": cover,
            "url": item.get("url")
        })

    return jsonify({
        "pagination": data.get("pagination", {}),
        "results": results
    })


# Статистика конкретной публикации
@app.get("/api/stats/<item_id>")
def stats(item_id):
    uuid_pattern = (
        r"[a-fA-F0-9]{8}"
        r"(?:-[a-fA-F0-9]{4}){3}"
        r"-[a-fA-F0-9]{12}"
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

    keys = (
        "content_viewing",
        "content_like_like",
        "content_like_dislike",
        "content_comments",
        "content_hidden"
    )

    return jsonify({
        key: data.get(key, 0)
        for key in keys
    })


# Диагностика доступа к Tach
@app.get("/api/diagnostics")
def diagnostics():
    params = {
        "page_size": 1,
        "order": "-pinned,-published",
        "filter": json.dumps({
            "user": USER_ID,
            "type__in": [4],
            "markers__in": [2]
        }, separators=(",", ":")),
        "page": 1
    }

    checks = {
        "profile": (
            f"/api/v1/user/{USER_ID}/",
            None
        ),
        "posts": (
            "/api/v1/usercontent/",
            params
        ),
        "counters": (
            "/api/v1/counters/usercontent/"
            "b3fd8adf-89c3-40a4-943f-8258b0225371/",
            None
        )
    }

    results = {}

    for name, (path, query_params) in checks.items():
        data, error = fetch(path, query_params)

        if error:
            results[name] = {
                "status": error.get("tach_status"),
                "content_type": error.get(
                    "tach_content_type", ""
                ),
                "message": error.get(
                    "tach_message",
                    error.get("error", "")
                )
            }
        else:
            results[name] = {
                "status": 200,
                "content_type": "application/json",
                "message": "OK"
            }

    return jsonify({
        "version": VERSION,
        "checks": results
    })


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "10000"))
    )
