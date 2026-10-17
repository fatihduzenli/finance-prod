"""The company settings row's first values (docs/qa/PLAN-012c-design-pass.md D2).

Migration ``0045_company_settings`` seeds the row with its own copy of these values;
:meth:`ledger.models.CompanySettings.current` uses them only when the row is missing (a
test database that was flushed). The payment processor's display name lives here and in
the row, never in a template or a sentence: every screen reads ``company.processor_name``.
"""

from __future__ import annotations

from typing import Final

SEED: Final[dict[str, str]] = {
    "name": "Monvera",
    "short_name": "Monvera",
    "support_email": "",
    "time_zone": "America/New_York",
    "logo": "",
    "processor_name": "Stax",
}
