
@app.get("/api/videos")
def videos():
    nickname = request.args.get(
        "nickname", "vladislove"
    ).strip().lower()

    if nickname != "vladislove":
        return jsonify({
            "error": "Пока поддерживается профиль vladislove"
        }), 400

    page = request.args.get("page", "1")
    content_type = request.args.get("type", "4")

    if not page.isdigit() or not 1 <= int(page) <= 100:
        return jsonify({"error": "Некорректная страница"}), 400

    if content_type not in ("4", "5"):
        return jsonify({"error": "Допустимые типы: 4 и 5"}), 400

    params = {
        "page_size": 10,
        "order": "-pinned,-published",
        "filter": json.dumps({
            "user": USER_ID,
            "type__in": [int(content_type)],
            "markers__in": [2]
        }, separators=(",", ":")),
        "page": int(page)
    }

    data, error = fetch("/api/v1/usercontent/", params)

    if error:
        return jsonify(error), 502

    result = []

    for item in data.get("results", []):
        content = item.get("content") or {}
        body = content.get("text") or ""
        images = content.get("images_ext") or []

        result.append({
            "id": item.get("id"),
            "type": item.get("type"),
            "title": (
                content.get("title")
                or body[:120]
                or "Без названия"
            ),
            "text": body,
            "published": item.get("published"),
            "cover": (
                content.get("cover_url")
                or (images[0].get("url") if images else None)
            ),
            "url": item.get("url")
        })

    return jsonify({
        "pagination": data.get("pagination", {}),
        "results": result
    })
