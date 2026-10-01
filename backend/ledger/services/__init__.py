"""Business services of the ledger (docs/qa/PLAN-007-leases-and-rent-run.md §4.2).

Views call these functions; these functions are the only code of the ledger screens
that obtains a payment provider (``payments.get_provider()``) and calls it.
"""
