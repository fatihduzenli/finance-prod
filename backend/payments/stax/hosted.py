"""Stax.js hosted bank-account fields (docs/qa/PLAN-014 C7).

The bank-entry page loads Stax.js from Stax's CDN with the merchant's **web payments
token** (``STAX_WEB_PAYMENTS_TOKEN``, Key Vault ``stax-web-payments-token``). The script
shows the account holder, routing and account fields in the processor's own frames,
tokenizes the account under the lease's customer and hands the page the new payment
method's id, which is all the app receives (Rule 1).

TODO-VERIFY-066: ``docs/`` names no Stax.js URL; the CDN URL below is the one Stax publishes
for Stax.js (formerly Fattmerchant.js) and is not verified here, nor is the bank-field API
the page script uses (``ledger/static/ledger/bank_entry.js``), nor the API host the script
calls. The sandbox cannot tokenize a bank account ("no tokenization gateway found",
``docs/STATUS.md``), so none of this can be exercised on staging.
"""

from __future__ import annotations

from payments.hosted_fields import HostedFieldsConfig

#: TODO-VERIFY-066: Stax.js from Stax's CDN.
SCRIPT_URL = "https://fattjs.fattpay.com/js/fattmerchant.js"
#: The origin the script and its frames are served from (the page's CSP names it).
PROVIDER_HOST = "https://fattjs.fattpay.com"


class StaxHostedFields:
    """Stax.js with the merchant's web payments token."""

    def __init__(self, web_payments_token: str) -> None:
        self._token = web_payments_token

    def __repr__(self) -> str:
        return "StaxHostedFields()"

    def available(self) -> bool:
        return bool(self._token)

    def config_for(self, customer_external_id: str) -> HostedFieldsConfig | None:
        if not self._token or not customer_external_id:
            return None
        return HostedFieldsConfig(
            script_url=SCRIPT_URL,
            web_payments_token=self._token,
            customer_external_id=customer_external_id,
            provider_host=PROVIDER_HOST,
        )
