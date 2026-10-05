## QA Report — PLAN-010c: documents on stores and leases; store-cost corrections; report timeout

**Date:** 2026-10-05 · **QA owner:** Fable · **Implementer:** Opus
**Plan:** `docs/qa/PLAN-010c-documents-and-cost-corrections.md` (§9 lists the amendments made
during QA) · **Decision record:** ADR-0008 · docs/CLAUDE.md Rule 1 gained the file policy.

**Scope.** New package `backend/documents_storage/` (interface, Azure Blob adapter by managed
identity, in-memory storage); `backend/finance/uploads.py` (in-memory upload handler with a hard
cap, per-route request limit, 413 middleware), settings and env guard (`DOCUMENTS_STORAGE_ACCOUNT_URL`,
`DOCUMENTS_CONTAINER`, `AZURE_CLIENT_ID`, `REPORT_STATEMENT_TIMEOUT_MS`); `ledger`: `Document`
reshaped (migration 0015), `StoreCost` corrections (0016), services `documents`, `maintenance`,
`store_costs.correct_cost|close_cost`, P&L timeout, `visible_text` on the older mandatory notes;
views `documents`, additions to `stores`, `leases`, `reports`; templates and one static script;
registry. Infra: `infra/modules/storage.bicep`, wiring in `main.bicep` and `containerAppWeb.bicep`;
`.env.example`. Dependencies, pinned: `azure-storage-blob==12.31.0` (MIT), `azure-identity==1.26.0`
(MIT), `pypdf==6.19.0` (BSD-3); transitive: azure-core, cryptography, cffi, pycparser, isodate,
msal, msal-extensions, pyjwt (MIT, BSD or Apache-2.0); `requests` becomes a runtime dependency
through azure-core. No Stax change; the worker and the scheduled job are untouched.

| Command | Database | Exit | Result |
|---|---|---|---|
| `uv run ruff check .`, `uv run ruff format --check .` | n/a | 0, 0 | clean, 311 files |
| `uv run mypy` (strict) | n/a | 0 | 249 source files |
| `uv lock --check` | n/a | 0 | 62 packages resolved, lock current |
| `az bicep build --file infra/main.bicep` | n/a | 0 | no warnings |
| `makemigrations --check --dry-run` | both | 0 | no changes |
| `uv run pytest -m "not e2e" -q -o faulthandler_timeout=300` | PostgreSQL 16 | 0 | 3,704 passed, 3 skipped |
| same | sqlite | 0 | 3,240 passed, 467 skipped |
| `uv run pytest -m e2e -q` | PostgreSQL 16 | 0 | 5 passed (upload by input and by drop; PNG and PDF preview in Chromium) |
| `bash scripts/check-no-secrets.sh` | n/a | 0 | clean |
| Pattern scan over the diff and new files | n/a | — | one hit, a test's assembled placeholder for the detector self-test; no secret |
| `az deployment group what-if` (read-only, staging parameters, live image) | n/a | 0 | Create storage account, blob service, container `documents`, one role assignment; Modify web containers (env); the rest is the usual read-only noise |

**Tests.** 3,234 → 3,704 on PostgreSQL. Opus: `test_documents.py`, `test_documents_storage.py`,
`test_infra_storage.py`, `test_store_cost_corrections.py`, `test_report_timeout.py`,
`test_invisible_notes.py`, e2e. QA-owned `test_documents_adversarial.py` (27 cases): numbers in
PDF form-field values, annotations and document properties; in DOCX properties and alt text;
numbers split across lines, pages, runs and paragraphs, with zero-width and full-width
characters (differential against the text guard); an encrypted zip and a PDF/HTML polyglot;
card numbers and markup in filenames; role sweep for viewer, billing, no-role and anonymous;
deleted documents and guessed or malformed IDs; audit of every read and no filename text on a
refusal; a blob lost from storage. Fences: no `azure.*` import outside `documents_storage`; the
SDK is never built with a key, connection string or SAS; no temporary file during an upload.

**Findings.** BLOCKER 0. **MAJOR 3, resolved:** (1) the guard read only PDF page text and DOCX
run text, so a fillable bank form's field values, annotations and document properties were
stored unchecked (found by the QA tests; D6 widened, embedded files refused); (2) the same for
DOCX properties and attribute-held text; (3) the preview's `sandbox` policy would have blanked
PDFs in the browser's viewer (PDF previews now use a no-script policy without `sandbox`).
**MINOR 6, resolved:** the 26 MiB request limit applied to every route including the login form;
`sha256` had no shape CHECK; unbounded SDK time under the owner row lock (now about 25–33 s at
most, no retries); the worker parity test contradicted D1 (test adjusted); embedded packages;
the schema test's identifier list. **Carried items closed here:** report statement timeout and
post-import ANALYZE (owner deadline, before Prompt 13); invisible-character notes (deferred from
QA-010). **Still deferred by written decision:** the ledger "Deposits held" total over the
filtered set (QA-010b).

**Residual risk.**
- **Images and scanned PDFs are not checked** (stated policy, ADR-0008). A number split across
  two separate properties or fields is not joined; a broken PDF name tree is skipped, so an
  attachment hidden behind one would not be detected; PDF streams with filters other than Flate
  are not read for metadata. The guard is a strong filter, not a proof.
- **Nothing ran against Azure Storage.** Managed-identity authentication, the role assignment
  and the SDK timeouts are verified by configuration and unit tests only; the first real upload
  is the owner's click. A new role assignment can take a few minutes to take effect.
- **PDF preview in the owner's browser** is verified in Playwright's Chromium only.
- **No virus scanning; public storage endpoint with Entra-only auth; locally redundant
  storage** (owner options recorded in ADR-0008).
- 25 MB uploads are held in memory; pypdf on a hostile file is bounded by size and page caps,
  not by a timer.
- All test files are built in code; no file produced by Word or Acrobat was tested.
- A pre-existing `listKeys()` for the Log Analytics workspace key remains in
  `containerAppsEnvironment.bicep` (not part of this change; the value never reaches the app).

**Verdict: PASS** (pending the staging done-when after the push and the infrastructure
deployment).
