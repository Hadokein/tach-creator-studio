
import json
import os
import re
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urljoin

import requests
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
TACH = "https://tach.id"

USER_ID = "7fd088f2-79a4-4ec4-a2a1-f1b45724fb99"
NICKNAME = "vladislove"
SAMPLE_CONTENT_ID = "b3fd8adf-89c3-40a4-943f-8258b0225371"
PUBLIC_POST_PATH = "/p/vladislove/post/66ff2cb9"
VERSION = "2.0"

SESSION = requests.Session()
SESSION.headers.update({
    "Accept": "application/json",
    "User-Agent": "TachCreatorStudio/2.0"
})


def classify_error(response):
    body = response.text[:2048].lower()

    if "no authorization header found" in body:
        message = "no Authorization header found"
    elif "unauthorized" in body:
        message = "unauthorized"
    elif "forbidden" in body:
        message = "forbidden"
    elif "access denied" in body:
        message = "access denied"
    else:
        message = "unrecognized response"

    return {
        "tach_status": response.status_code,
        "tach_content_type": response.headers.get(
            "Content-Type", ""
        ).split(";")[0],
        "tach_message": message
    }


@app.get("/")
def home():
    if not (BASE_DIR / "index.html").is_file():
        return jsonify({
            "error": "index.html не найден рядом с app.py"
        }), 404

    return send_from_directory(str(BASE_DIR), "index.html")


@app.get("/tach_browser_test.html")
def browser_test():
    filename = "tach_browser_test.html"

    if not (BASE_DIR / filename).is_file():
        return jsonify({
            "error": f"{filename} не найден"
        }), 404

    return send_from_directory(str(BASE_DIR), filename)


class PublicPageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title_active = False
        self.title = ""
        self.meta = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)

        if tag == "title":
            self.title_active = True

        if tag == "meta":
            key = (
                attributes.get("property")
                or attributes.get("name")
            )

            if key in (
                "description",
                "og:title",
                "og:description",
                "og:image"
            ):
                self.meta[key] = attributes.get(
                    "content", ""
                )[:500]

    def handle_endtag(self, tag):
        if tag == "title":
            self.title_active = False

    def handle_data(self, data):
        if self.title_active:
            self.title += data


class ScriptInventoryParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag != "script":
            return

        src = dict(attrs).get("src", "")

        if (
            src.startswith("/_next/static/")
            and src.endswith(".js")
            and src not in self.scripts
        ):
            self.scripts.append(src)


def get_public_page():
    return SESSION.get(
        TACH + PUBLIC_POST_PATH,
        timeout=15,
        headers={"Accept": "text/html"}
    )


def get_scripts():
    response = get_public_page()
    response.raise_for_status()

    parser = ScriptInventoryParser()
    parser.feed(response.text)

    return parser.scripts


def sanitize(fragment):
    fragment = re.sub(
        r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+",
        r"\1[REDACTED]",
        fragment
    )

    fragment = re.sub(
        r"""(?i)((?:access|refresh|id)[_-]?token\s*[:=]\s*["'])([^"']+)""",
        r"\1[REDACTED]",
        fragment
    )

    fragment = re.sub(
        r"""(?i)(authorization\s*[:=]\s*["'])([^"']+)""",
        r"\1[REDACTED]",
        fragment
    )

    return fragment


@app.get("/api/health")
def health():
    return jsonify({
        "ok": True,
        "service": "tach-creator-studio",
        "version": VERSION,
        "index_exists": (
            BASE_DIR / "index.html"
        ).is_file(),
        "browser_test_exists": (
            BASE_DIR / "tach_browser_test.html"
        ).is_file()
    })


@app.get("/api/public-page-probe")
def public_page_probe():
    try:
        response = get_public_page()

        result = {
            "version": VERSION,
            "url": response.url,
            "status": response.status_code,
            "content_type": response.headers.get(
                "Content-Type", ""
            ).split(";")[0]
        }

        if not response.ok:
            result["message"] = classify_error(
                response
            )["tach_message"]
            return jsonify(result)

        html = response.text

        parser = PublicPageParser()
        parser.feed(html)

        result.update({
            "title": parser.title.strip()[:200],
            "meta": parser.meta,
            "html_bytes": len(response.content),
            "next_data_present": (
                "__next_f.push" in html
                or "__NEXT_DATA__" in html
            ),
            "counter_fields_in_html": {
                key: key in html
                for key in (
                    "content_viewing",
                    "content_like_like",
                    "content_comments"
                )
            }
        })

        return jsonify(result)

    except requests.RequestException as exc:
        return jsonify({
            "version": VERSION,
            "error": type(exc).__name__
        }), 502


@app.get("/api/source-inspect")
def source_inspect():
    try:
        page = get_public_page()
        page.raise_for_status()

        inventory = ScriptInventoryParser()
        inventory.feed(page.text)

        scripts = sorted(
            inventory.scripts,
            key=lambda src: (
                0 if "/app/" in src
                else 1 if "main-app" in src
                else 2,
                src
            )
        )[:8]

        markers = (
            "counters/usercontent",
            "content_viewing",
            "authorization",
            "bearer",
            "/api/v1/",
            "usercontent"
        )

        bundles = []

        for src in scripts:
            entry = {"file": src}

            try:
                response = SESSION.get(
                    urljoin(TACH, src),
                    timeout=10
                )

                entry["status"] = response.status_code
                entry["bytes"] = len(response.content)

                if (
                    response.ok
                    and len(response.content) <= 5000000
                ):
                    lower = response.text.lower()
                    entry["markers"] = {
                        marker: lower.count(marker)
                        for marker in markers
                    }

            except requests.RequestException as exc:
                entry["error"] = type(exc).__name__

            bundles.append(entry)

        html = page.text

        return jsonify({
            "version": VERSION,
            "page_status": page.status_code,
            "html": {
                "bytes": len(page.content),
                "next_f_blocks": html.count(
                    "self.__next_f.push"
                ),
                "js_files_found": len(
                    inventory.scripts
                ),
                "post_id_present": (
                    "66ff2cb9" in html
                ),
                "content_uuid_present": (
                    SAMPLE_CONTENT_ID in html
                ),
                "profile_uuid_present": (
                    USER_ID in html
                ),
                "counter_names": {
                    key: key in html
                    for key in (
                        "content_viewing",
                        "content_like_like",
                        "content_comments"
                    )
                }
            },
            "bundles": bundles
        })

    except requests.RequestException as exc:
        return jsonify({
            "version": VERSION,
            "error": type(exc).__name__
        }), 502


@app.get("/api/source-inspect-deep")
def source_inspect_deep():
    try:
        batch = int(request.args.get("batch", "1"))
    except ValueError:
        return jsonify({
            "error": "batch must be 1 through 5"
        }), 400

    if batch not in range(1, 6):
        return jsonify({
            "error": "batch must be 1 through 5"
        }), 400

    try:
        files = get_scripts()

        selected = files[
            (batch - 1) * 8:batch * 8
        ]

        markers = (
            "usercontent",
            "counter",
            "authorization",
            "bearer",
            "api/v1",
            "content_viewing",
            "fetch(",
            "axios",
            "interceptor",
            "token"
        )

        entries = []

        for src in selected:
            entry = {"file": src}

            try:
                response = SESSION.get(
                    TACH + src,
                    timeout=10
                )

                entry["status"] = response.status_code
                entry["bytes"] = len(response.content)

                if (
                    response.ok
                    and len(response.content) <= 5000000
                ):
                    source = response.text
                    lower = source.lower()

                    entry["markers"] = {
                        marker: lower.count(marker)
                        for marker in markers
                    }

                    excerpts = []

                    for marker in (
                        "usercontent",
                        "counter",
                        "api/v1",
                        "content_viewing"
                    ):
                        start = 0

                        for _ in range(3):
                            pos = lower.find(
                                marker, start
                            )

                            if pos < 0:
                                break

                            fragment = source[
                                max(0, pos - 100):
                                pos + 160
                            ]

                            excerpts.append({
                                "marker": marker,
                                "context": sanitize(fragment)
                            })

                            start = pos + len(marker)

                    entry["excerpts"] = excerpts[:8]

            except requests.RequestException as exc:
                entry["error"] = type(exc).__name__

            entries.append(entry)

        return jsonify({
            "version": VERSION,
            "batch": batch,
            "total_files": len(files),
            "files_in_batch": len(selected),
            "bundles": entries
        })

    except requests.RequestException as exc:
        return jsonify({
            "version": VERSION,
            "error": type(exc).__name__
        }), 502


@app.get("/api/auth-client-inspect")
def auth_client_inspect():
    targets = {
        "api_schema": (
            "/_next/static/chunks/"
            "422-5df1ddcb9ef44d09.js"
        ),
        "http_library": (
            "/_next/static/chunks/"
            "4385-e13dd9f6eb36281f.js"
        ),
        "counter_ui": (
            "/_next/static/chunks/"
            "3943-eee345068b2fc289.js"
        )
    }

    needles = {
        "counter_method":
            "oneCountersUsercontentApiV1CountersUsercontentUsercontentIdGet",
        "counter_path":
            "/counters/usercontent/",
        "http_bearer_schema":
            "HTTPBearer",
        "authorization_header":
            "authorization",
        "axios_interceptor":
            "interceptors.request",
        "access_token_refresh":
            "getAccessTokenPayloadSecurely",
        "api_base_url":
            "apiUrl"
    }

    output = {}

    for name, path in targets.items():
        try:
            response = SESSION.get(
                TACH + path,
                timeout=15
            )

            entry = {
                "status": response.status_code,
                "bytes": len(response.content)
            }

            if (
                response.ok
                and len(response.content) <= 2000000
            ):
                source = response.text
                lower = source.lower()

                matches = {}

                for label, needle in needles.items():
                    positions = [
                        match.start()
                        for match in re.finditer(
                            re.escape(needle.lower()),
                            lower
                        )
                    ]

                    matches[label] = {
                        "count": len(positions),
                        "offsets": positions[:5]
                    }

                entry["matches"] = matches

                contexts = []

                for label in (
                    "counter_method",
                    "counter_path",
                    "http_bearer_schema"
                ):
                    needle = needles[label]
                    pos = lower.find(needle.lower())

                    if pos >= 0:
                        fragment = source[
                            max(0, pos - 100):
                            pos + len(needle) + 180
                        ]

                        contexts.append({
                            "kind": label,
                            "excerpt": sanitize(fragment)
                        })

                entry["route_excerpts"] = contexts

            output[name] = entry

        except requests.RequestException as exc:
            output[name] = {
                "error": type(exc).__name__
            }

    return jsonify({
        "version": VERSION,
        "bundles": output,
        "note": (
            "Public JavaScript analysis. "
            "No credentials requested."
        )
    })


# Диагностика конфигурации API-клиента Tach
@app.get("/api/client-config-inspect")
def client_config_inspect():
    path = (
        "/_next/static/chunks/"
        "422-5df1ddcb9ef44d09.js"
    )

    try:
        response = SESSION.get(
            TACH + path,
            timeout=18
        )

        if not response.ok:
            return jsonify({
                "version": VERSION,
                "status": response.status_code,
                "error": "JavaScript unavailable"
            }), 502

        if len(response.content) > 1000000:
            return jsonify({
                "error": "Unexpected bundle size"
            }), 502

        source = response.text
        lower = source.lower()

        needles = {
            "counter_endpoint":
                "oneCountersUsercontentApiV1CountersUsercontentUsercontentIdGet",
            "authorization":
                "authorization",
            "api_url":
                "apiUrl",
            "bearer_schema":
                "HTTPBearer"
        }

        offsets = {}

        for key, needle in needles.items():
            offsets[key] = [
                match.start()
                for match in re.finditer(
                    re.escape(needle.lower()),
                    lower
                )
            ]

        excerpts = []

        for label in (
            "counter_endpoint",
            "authorization",
            "api_url"
        ):
            for offset in offsets[label][:8]:
                left = (
                    220 if label == "authorization"
                    else 280
                )

                right = (
                    380 if label == "authorization"
                    else 450
                )

                fragment = source[
                    max(0, offset - left):
                    min(len(source), offset + right)
                ]

                excerpts.append({
                    "kind": label,
                    "offset": offset,
                    "context": sanitize(fragment)
                })

        counter_offsets = offsets["counter_endpoint"]

        if counter_offsets:
            pos = counter_offsets[0]
            counter_schema = source[
                pos:pos + 250
            ]

            explicit_bearer = (
                'security:["HTTPBearer"]'
                in counter_schema
            )
        else:
            explicit_bearer = None

        return jsonify({
            "version": VERSION,
            "status": response.status_code,
            "bundle_bytes": len(response.content),
            "counter_endpoint_has_explicit_bearer":
                explicit_bearer,
            "counts": {
                key: len(value)
                for key, value in offsets.items()
            },
            "excerpts": excerpts,
            "note": (
                "Static public client code only. "
                "No credentials requested."
            )
        })

    except requests.RequestException as exc:
        return jsonify({
            "version": VERSION,
            "error": type(exc).__name__
        }), 502


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
        response = exc.response

        status = (
            response.status_code
            if response is not None
            else None
        )

        info = (
            classify_error(response)
            if response is not None
            else {"tach_status": status}
        )

        return None, {
            "error": f"Tach вернул HTTP {status}",
            **info
        }

    except requests.Timeout:
        return None, {
            "error": "Истекло время ожидания Tach"
        }

    except requests.RequestException as exc:
        return None, {
            "error": "Ошибка подключения к Tach",
            "type": type(exc).__name__
        }

    except ValueError:
        return None, {
            "error": "Tach вернул не JSON"
        }


@app.get("/api/videos")
def videos():
    nickname = request.args.get(
        "nickname", NICKNAME
    ).strip().lower()

    if nickname != NICKNAME:
        return jsonify({
            "error": "Пока поддерживается только vladislove"
        }), 400

    page_str = request.args.get("page", "1")
    content_type = request.args.get("type", "5")

    if (
        not page_str.isdigit()
        or not 1 <= int(page_str) <= 100
    ):
        return jsonify({
            "error": "Некорректный номер страницы"
        }), 400

    if content_type not in ("4", "5"):
        return jsonify({
            "error": "Допустимые типы: 4 и 5"
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
            first = images[0]

            if isinstance(first, dict):
                cover = first.get("url")

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


@app.get("/api/stats/<item_id>")
def stats(item_id):
    pattern = (
        r"[a-fA-F0-9]{8}"
        r"(?:-[a-fA-F0-9]{4}){3}"
        r"-[a-fA-F0-9]{12}"
    )

    if not re.fullmatch(pattern, item_id):
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


@app.get("/api/diagnostics")
def diagnostics():
    checks = [
        (
            "profile",
            f"/api/v1/user/{USER_ID}/",
            None
        ),
        (
            "posts",
            "/api/v1/usercontent/",
            {
                "page_size": 1,
                "page": 1,
                "order": "-pinned,-published",
                "filter": json.dumps({
                    "user": USER_ID,
                    "type__in": [4],
                    "markers__in": [2]
                }, separators=(",", ":"))
            }
        ),
        (
            "counters",
            f"/api/v1/counters/usercontent/{SAMPLE_CONTENT_ID}/",
            None
        )
    ]

    results = {}

    for name, path, params in checks:
        try:
            response = SESSION.get(
                TACH + path,
                params=params,
                timeout=10
            )

            entry = {
                "status": response.status_code,
                "content_type": response.headers.get(
                    "Content-Type", ""
                ).split(";")[0]
            }

            if response.status_code >= 400:
                entry["message"] = classify_error(
                    response
                )["tach_message"]
            else:
                entry["message"] = "ok"

            results[name] = entry

        except requests.RequestException as exc:
            results[name] = {
                "status": None,
                "message": type(exc).__name__
            }

    return jsonify({
        "version": VERSION,
        "checks": results
    })


@app.get("/api/session-source-inspect")
def session_source_inspect():
    path = (
        "/_next/static/chunks/"
        "422-5df1ddcb9ef44d09.js"
    )

    try:
        response = SESSION.get(
            TACH + path,
            timeout=18
        )
        response.raise_for_status()

        source = response.text

        modules = {}

        for module_id, label in (
            ("60051", "auth_helper"),
            ("38468", "app_version")
        ):
            match = re.search(
                r"(?<!\d)" + module_id + r":\s*\(",
                source
            )

            if match:
                start = match.start()
                modules[label] = {
                    "found": True,
                    "offset": start,
                    "source_excerpt": source[
                        start:start + 2800
                    ]
                }
            else:
                modules[label] = {
                    "found": False
                }

        return jsonify({
            "version": "2.1",
            "status": response.status_code,
            "modules": modules,
            "note": (
                "Public JavaScript source only. "
                "No user credentials requested."
            )
        })

    except requests.RequestException as exc:
        return jsonify({
            "error": type(exc).__name__
        }), 502

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "10000"))
    )
