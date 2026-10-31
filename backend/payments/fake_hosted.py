"""Fake hosted fields for local development and tests (docs/qa/PLAN-014 C7).

Selected only when ``STAX_PROVIDER=fake`` (accepted by the environment guard only when
``APP_ENV=local``). The bank-entry page then shows a plain form with one field,
``payment_method_id``: a method id the fake provider knows (its ``add_payment_method``
test control) is verified by the attach job like a real one. No script is loaded.
"""

from __future__ import annotations

from payments.hosted_fields import HostedFieldsConfig


class FakeHostedFields:
    def available(self) -> bool:
        return True

    def config_for(self, customer_external_id: str) -> HostedFieldsConfig | None:
        if not customer_external_id:
            return None
        return HostedFieldsConfig(
            script_url="",
            web_payments_token="",
            customer_external_id=customer_external_id,
            provider_host="",
        )
