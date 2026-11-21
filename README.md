# Tach Creator Studio

Тестовый проект для календаря стримов и аналитики публичных публикаций Tach.

## Render

- Runtime: Python
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn app:app`
- Plan: Free (если доступен)

Проверка сервера: `/api/health`.

API Tach не является подтверждённым публичным API для сторонних разработчиков. Доступность с Render не гарантируется. Текущая версия поддерживает только тестовый профиль `vladislove` и видео типа 5. Никаких токенов, cookies и ключей трансляций не требуется.

## Загрузка через iPhone

Распакуйте ZIP в приложении «Файлы». В GitHub откройте репозиторий → Add file → Upload files и загрузите `app.py`, `requirements.txt`, `render.yaml`, `static/index.html` (с сохранением папки static). Если GitHub на iPhone не принимает папку, создайте `static/index.html` через Add file → Create new file и вставьте содержимое.
