# PLAN-003b — Refused-host requests log one warning line

**Date:** 2026-09-29 · **Planner / QA owner:** Fable · **Implementer:** Opus
**Origin:** owner instruction of 2026-09-29; QA-003a MINOR finding 2, observed on staging.

## 1. Restated objective

A request whose Host header is not in `ALLOWED_HOSTS` (for example the default Azure
hostname, or a scanner) is already refused with 400. Django reports it on the logger
`django.security.DisallowedHost` at ERROR with a full traceback, about 15 log lines per
request. After this change the same event is written as **one WARNING line with no
traceback**. The refusal itself does not change.

## 2. Affected packages

Root project only. Touched: `backend/finance/log_redaction.py`, `backend/finance/settings.py`
(`LOGGING` only), `backend/tests/test_logging.py`, `README.md` (one sentence).
**Out of scope:** `ALLOWED_HOSTS`, middleware, `infra/`, `.github/`, dependencies, the 4xx
warning volume, every other logger.

## 3. Interface contract

`class RefusedHostFilter(logging.Filter)` in `backend/finance/log_redaction.py`, attached in
`LOGGING` to the logger `django.security.DisallowedHost` only.

For every record it sees, `filter` returns `True` after changing the record:
- `levelno = logging.WARNING`, `levelname = "WARNING"`.
- `exc_info = None`, `exc_text = None`, `stack_info = None`.
- The message becomes `Refused request: host not allowed: <host>` where `<host>` is the
  rejected Host header taken from the original message, with every character outside
  `[A-Za-z0-9.:\-\[\]_]` replaced by `?` and the result cut to 100 characters. If no host
  can be extracted the message is `Refused request: host not allowed`. `args` is cleared.
- The Host header is attacker-controlled. No part of the original message other than the
  sanitised host may reach the log.

The record still passes through the root handler and `RedactingFormatter`. Other
`django.security.*` loggers and `django.request` are untouched and keep their tracebacks.

## 4. Work breakdown

| Task | Files | Done when |
|---|---|---|
| T1 | `backend/finance/log_redaction.py`, `backend/finance/settings.py` | contract holds |
| T2 | `backend/tests/test_logging.py` (append only) | Opus rows of §5 pass |
| T3 | `README.md`, one sentence in "Start-up and logging" | matches behaviour |

## 5. Test plan

| # | Case | Author |
|---|---|---|
| 1 | Request with a disallowed Host: response 400, exactly one log event, level WARNING, no `Traceback` | Opus |
| 2 | The line names the refused host | Opus |
| 3 | A view that raises still logs ERROR with traceback (no regression) | Opus (existing test stays green) |
| 4 | Another `django.security` logger, e.g. `django.security.SuspiciousOperation`, is unchanged | Opus |
| 5 | Host with control characters, quotes, spaces, non-ASCII, ANSI escape and 5,000 characters: one line, sanitised, at most 100 host characters | Fable |
| 6 | Host equal to a configured secret value is still redacted | Fable |
| 7 | Record without a parsable host, and record with `%` arguments | Fable |
| 8 | Full suite, lint, format, types | Fable |

## 6. Risk register

| Risk | Treatment |
|---|---|
| Log injection through the Host header | Character whitelist and length cap; test 5 |
| Hiding a real attack signal | The event is kept, as a WARNING with the host; only the traceback is dropped |
| Filter attached too broadly and hiding real errors | Attached to the one logger by name; test 3 and 4 |
| Filter mutates a record shared with Sentry | Sentry is off on staging; when on, it receives the downgraded record, which is the intended level |

## 7. Acceptance criteria

1. `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy` exit 0.
2. Both pytest commands exit 0; no existing test edited, removed or skipped.
3. Every row of §5 has a passing test.
4. Diff touches only the files in §4. No dependency change.
5. After deployment, a request to the default Azure hostname produces one WARNING line in the
   container log. Verified by Fable after an approved push.
