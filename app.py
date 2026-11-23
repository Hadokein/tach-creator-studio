"""Gunicorn entry point for existing Render services."""
import os

if os.getenv('DATABASE_URL'):
    from postgres_app import app
else:
    from standalone import app

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '10000')))
