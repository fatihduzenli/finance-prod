"""The portal app's configuration: on ready, the one-time links of the portal messages are
registered with the messaging engine (:data:`ledger.services.messaging.ONE_TIME_LINKS`)."""

from __future__ import annotations

from django.apps import AppConfig


class PortalConfig(AppConfig):
    name = "portal"
    verbose_name = "Operator portal"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from portal import invitations

        invitations.register_links()
