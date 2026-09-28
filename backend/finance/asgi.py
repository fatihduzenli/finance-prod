"""ASGI entry point, kept alongside wsgi.py for tooling that expects it."""

from __future__ import annotations

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "finance.settings")

application = get_asgi_application()
