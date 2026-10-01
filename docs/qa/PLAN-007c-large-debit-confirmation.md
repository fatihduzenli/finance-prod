# PLAN-007c — Large-debit warning with admin confirmation; confirmed Stax limit as a hard block

**Date:** 2026-10-01 · **Planner / QA owner:** Fable · **Implementer:** Opus
**Origin:** owner, Prompt 7 walk-through step 6: the placeholder threshold hard-blocked enrolment at
$2,100 while the median rent is $3,800.

## 1. Rules

| Setting | Meaning | Default |
|---|---|---|
| `LARGE_DEBIT_WARNING_AMOUNT` (exists) | Above this, enabling autopay needs an explicit admin confirmation; the run page tags the charge "Large debit" | `2000.00` |
| `STAX_MAX_TXN_AMOUNT` (new) | The ACH per-transaction limit **confirmed by Stax** for this environment. Empty means unknown. When set, any debit above it is **hard-blocked**: no confirmation can pass it | empty |

- Enable autopay, per debit amount: above `STAX_MAX_TXN_AMOUNT` when set → refused, message
  "Exceeds the confirmed provider limit of $X. Autopay cannot be enabled." No call. Else above
  `LARGE_DEBIT_WARNING_AMOUNT` → the enrolment page shows the warning and a checkbox
  "I confirm this debit of $A exceeds the warning threshold of $T and may be enabled";
  without the checkbox the POST is refused with that sentence and no call is made; with it the
  create proceeds and an audit row `lease.autopay_large_debit_confirmed` records
  `{"amount", "threshold", "confirmed_by": actor id}` before the create. Only `admin` can confirm
  (enrolment is admin-only already).
- The run page keeps the "Large debit" tag and adds "exceeds confirmed limit" when the limit is
  set and exceeded; it creates charges regardless (charges are local), and `match` is unaffected.
- `STAX_MAX_TXN_AMOUNT`: `env_guard.py` (optional decimal string with two decimals, `> 0`),
  `settings.py`, `.env.example`, Bicep parameter `staxMaxTxnAmount` (default `''`) wired to the
  env var in `infra/main.bicep` and `infra/modules/containerAppWeb.bicep`; not set in
  `staging.bicepparam` until Stax answers Q25 (comment there pointing at the question).
- `docs/STAX_ANSWERS.md` Q2 is where the owner records the confirmed figure (Fable).

## 2. Tests

| # | Case | Author |
|---|---|---|
| 1 | Below warning: no checkbox needed, create proceeds. Above warning, no checkbox: refused, no call, fixed sentence. Above warning with checkbox: create proceeds, audit row with amount, threshold and actor, written before the create | Opus |
| 2 | Limit set and amount above it: refused even with the checkbox, no call, message names the limit. Limit set and amount between warning and limit: checkbox path works. Limit empty: no hard block at any amount | Opus |
| 3 | Twice-monthly: the per-debit amount (half) is what is compared | Opus |
| 4 | Env guard: valid values, invalid values (negative, text, three decimals) refused with value-free messages; empty accepted | Opus |
| 5 | Run page tags: "Large debit" and "exceeds confirmed limit" | Opus |
| 6 | Adversarial: checkbox value tampering (`"on"`, `"1"`, `"false"`, missing); confirmation posted by billing (403); confirmation replay after a failed create does not create twice; amounts exactly equal to the thresholds | Fable |
| 7 | Whole suite both engines, lint, format, types, Bicep compile, secret scan | Fable |
