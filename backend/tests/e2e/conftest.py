"""End-to-end test setup.

Playwright's sync API drives the browser from an event loop running in this thread,
so Django's "synchronous only" assertion fires when pytest-django creates the test
database while that loop exists. The escape hatch Django documents for exactly this
case is the variable below. It relaxes an assertion inside the test process only;
nothing in the application runs in an async context.
"""

from __future__ import annotations

import os

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")
