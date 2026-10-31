"""Bank deposits (docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md Batch I).

Credits that arrive in the business's own bank account (wires, Zelle, ACH credits, checks)
are read from the bank through a :class:`~banking.sources.BankTransactionSource` (Plaid, by
:mod:`banking.plaid.client`; the fake, from a fixture, in tests and locally), matched to the
open charges they pay (:mod:`banking.matching`) and recorded by staff on the Bank deposits
page through the store page's payment entry (:mod:`banking.services`). The module is dark
until an admin turns it on in Admin › Settings › Bank deposits, the Plaid keys are in the
environment and a bank account is connected.

This module names Plaid, the bank-data connector; it never names the payment processor
(docs/CLAUDE.md Rule 8).
"""
