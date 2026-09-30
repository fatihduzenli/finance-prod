"""The Stax adapter: the only package that knows Stax URLs, field names and statuses.

docs/CLAUDE.md Rule 8 and docs/qa/PLAN-005-stax-client.md §4.2. Nothing outside
``backend/payments/`` imports from here; a test enforces it.
"""
