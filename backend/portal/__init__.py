"""The operator portal (docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md Batch H).

Operators sign in at ``/portal/`` with a principal of their own (:class:`portal.models.PortalUser`),
never a staff ``User``: a separate cookie and session (:mod:`portal.middleware`), its own login,
invitation and reset (:mod:`portal.auth`, :mod:`portal.tokens`), and read-only pages whose every
query goes through :mod:`portal.services`, filtered by the portal user's operator.
"""
