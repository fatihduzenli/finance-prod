"""WSGI entry point for the web container."""

from __future__ import annotations

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "finance.settings")

application = get_wsgi_application()
