"""WSGI entry point for production servers (gunicorn / waitress)."""
from app import create_app

app = create_app()
