"""Webhook receiver and processor (docs/qa/PLAN-008-webhooks.md).

``receiver`` stores each delivery and answers at once; ``queue`` hands stored events to
one background thread per process; ``processor`` runs the handler for an event in its
own transaction; ``handlers`` maps event types to handler functions. Nothing here
imports ``payments.stax``: every provider call a handler makes goes through
``payments.get_provider()``.
"""
