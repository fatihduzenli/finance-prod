# PLAN-010c — Document uploads on stores and leases; store-cost corrections; report timeout

**Date:** 2026-10-05 · **Author:** Fable (QA owner) · **Implementer:** Opus
**Origin:** owner's Prompt 10c text and 10b finding of 2026-10-05; the owner's instruction of the
same day that the report statement timeout and the post-import ANALYZE land in this plan.

## 1. Restated objective

Staff need to keep a lease agreement, an amendment, a floor plan, a photo or a notice with the
store or lease it belongs to. This step fills the `documents` table reserved in Prompt 6: files
are uploaded from the store and lease pages (also while creating either), stored only in a
private Azure Blob Storage container reached with the app's managed identity, listed with a
preview for images and PDFs, downloaded only through the app by an authenticated user, and
soft-deleted by an admin; every upload, download and delete is audited. Because a document can
carry what the app must never hold, PDF and DOCX text is extracted at upload and run through the
existing card, routing and account detectors; a hit refuses the file and stores nothing, and the
upload box says plainly that bank forms and ACH authorisations stay in Stax or DocuSign. The same
push carries three smaller items: store cost rows become correctable by a superseding row and
closable with an end date (admin, audited, history kept); the P&L queries get a statement
timeout and the bulk importer refreshes planner statistics (owner deadline: before Prompt 13);
and the older mandatory notes stop accepting invisible characters.

## 2. Affected packages

`backend/ledger` (models, migrations, services, views, forms, templates, validators, URLs),
new `backend/documents_storage/` (the only code that talks to Blob Storage), `backend/finance`
(settings, env guard, upload handlers), `backend/accounts/registry.py`, `backend/tests`, `infra/`
(new storage module, role assignment, web env), `.env.example`, `docs/`. New dependencies,
approved here, pinned: `azure-storage-blob`, `azure-identity` (both MIT) and `pypdf` (BSD-3).
**Out of scope:** Stax, the worker, the scheduled job, DocuSign (the reserved `docusign` source
stays unused), OCR of images, virus scanning (recorded as a known gap), any public or shared link.

## 3. Decisions

| # | Decision | Reason |
|---|---|---|
| D1 | **Storage account** `st<prefix><unique>` in the environment's resource group (Bicep module `storage.bicep`): StorageV2, Standard_LRS, `allowBlobPublicAccess=false`, `allowSharedKeyAccess=false` (no account keys exist to leak), `minimumTlsVersion=TLS1_2`, HTTPS only, blob versioning on, blob soft delete 30 days, container soft delete 30 days, one private container `documents`. Role assignment **Storage Blob Data Contributor** on the container to the existing user-assigned identity. The web app gets `DOCUMENTS_STORAGE_ACCOUNT_URL`, `DOCUMENTS_CONTAINER` and `AZURE_CLIENT_ID` (the identity's client ID: an identifier, not a secret). The public endpoint stays reachable but accepts Entra tokens only; restricting it to the Container Apps subnet (service endpoint or private endpoint) is recorded as a hardening option for the owner, not built now | Owner item 1; one deployment; lowest risk to the existing network |
| D2 | **One storage interface**, `documents_storage.DocumentStorage` (`put(path, data, content_type)`, `open(path) -> iterator of chunks`, `exists(path)`), with `AzureBlobDocumentStorage` (`azure-storage-blob` + `ManagedIdentityCredential(client_id=AZURE_CLIENT_ID)`; no connection string, no key, no SAS) and `MemoryDocumentStorage` for tests and `APP_ENV=local` only. Nothing outside the package imports the Azure SDK (test-enforced, like the Stax rule). When `DOCUMENTS_STORAGE_ACCOUNT_URL` is empty outside local, the feature is **off**: the Documents sections say "Document storage is not configured" and every upload is refused; the app still boots (so the code can be pushed before the infrastructure exists) | Rule 8's pattern; lets push precede deploy |
| D3 | **Files never touch the container filesystem or the database.** `FILE_UPLOAD_HANDLERS` is the in-memory handler only; `DATA_UPLOAD_MAX_MEMORY_SIZE` and `FILE_UPLOAD_MAX_MEMORY_SIZE` are set so a 25 MB file is held in memory and anything larger is refused before it is read in full (`Content-Length` check first, then a hard cap while reading); the bytes go from memory to the blob in one `put`; a failed `put` writes no row. Downloads and previews are **streamed through the app** from the blob in chunks to an authenticated user; no SAS and no blob URL ever reaches a browser | Owner items 1 and 4; streaming needs no extra role and leaves no shareable link |
| D4 | **`documents` table reshaped** (it is empty: reserved, no UI). Columns: `store` FK and `lease` FK (nullable, exactly one set, CHECK), `kind` in {`lease_agreement`, `amendment`, `floor_plan`, `photo`, `notice`, `other`}, `source` (kept; `upload`), `original_filename` (sanitised, guarded), `content_type` (from the sniff), `size_bytes`, `blob_path` (unique; `documents/<store|lease>/<owner uuid>/<document uuid>`, never the filename), `sha256` (hex), `note` (guarded), `uploaded_by`, `uploaded_at`, `deleted_at`, `deleted_by`, `delete_note`. The reserved `operator` and `file_reference` columns are dropped by the migration, which first refuses to run if the table has any row. Unique `(owner, sha256)` among non-deleted rows: the same file twice on one record is refused with "already uploaded" | Owner item 2 |
| D5 | **Type rules.** Allowed: pdf, docx, jpg/jpeg, png, heic. The type is decided by content: `%PDF-` header; PNG and JPEG signatures; HEIC by its `ftyp` brand; DOCX as a zip holding `[Content_Types].xml` and `word/document.xml` and no `vbaProject.bin`. The extension must be one of the allowed ones and agree with the sniffed type; the stored `content_type` is the sniffed one. Size ≤ 25 MB (25 × 1024 × 1024 bytes) and > 0. Filename: base name only, control and path characters removed, Unicode normalised, at most 150 characters, never used in a path or header unescaped (`Content-Disposition` with RFC 5987 encoding) | Owner item 4 |
| D6 | **Bank-data guard on files.** PDF: `pypdf`, text of every page (cap 500 pages; an encrypted PDF or one pypdf cannot read is refused: "This PDF cannot be read, so it cannot be checked"); a PDF with no extractable text (a scan) is accepted like an image. DOCX: the text runs of `word/document.xml`, headers, footers, footnotes and comments, read with a size cap on each unpacked part and without an XML entity parser. The extracted text, the filename and the note go through `validate_no_payment_data` (card numbers by Luhn; a routing number beside an account-length digit run), after the existing normalisation, with digit runs joined across line and run breaks. A hit refuses the upload with the text guard's own sentence and stores nothing: no blob, no row, and the audit row records only "refused: payment data" with the filename's length, never its content. Images are not scanned; the upload box carries the notice "Do not upload bank forms, ACH authorisations, voided cheques or card details. Those stay in Stax or DocuSign." This is recorded as policy in `docs/CLAUDE.md` Rule 1 and `docs/DATA_OWNERSHIP.md` | Owner item 5 |
| D7 | **UI.** Store and lease detail pages get a Documents section: an upload box (file input plus drag and drop; one file per submit; plain form post, a small script only moves a dropped file into the input), kind, note, the notice of D6; the list (kind, filename, size, uploader, date, note) with **Preview** for PDF, PNG and JPEG (a preview route that streams the file `inline` with `Content-Security-Policy: sandbox`, `X-Content-Type-Options: nosniff` and the sniffed type; shown in a same-origin frame, so that one route answers `X-Frame-Options: SAMEORIGIN`), **Download** for all (`attachment`), **Delete** (admin, note required, soft). HEIC and DOCX are download-only. The store and lease **create** forms accept one optional file with kind and note; it is uploaded after the record is saved, and a refused file never undoes the record (the page says the record was created and why the file was not). Roles: billing and admin upload; viewer lists, previews and downloads; admin deletes and sees deleted rows | Owner item 3 |
| D8 | **Audit.** `document.uploaded`, `document.upload_refused` (reason code only), `document.downloaded`, `document.previewed`, `document.deleted`, each with actor, document ID, owner, kind, size and sha256 prefix; never the filename's text beyond its length when refused, never content | Owner item 2 |
| D9 | **Retention and backup** (documented in `docs/RUNBOOK.md` §12 and `docs/DATA_OWNERSHIP.md`): the database is backed up by Azure Database for PostgreSQL point-in-time restore, 7 days (as deployed). Blobs live in the storage account of D1, never in the database; the app never hard-deletes a blob (an app delete only sets `deleted_at`), and the platform keeps deleted or overwritten blobs for 30 days (soft delete and versioning). A database restored to any point in its 7-day window therefore finds every blob its rows name. Restore steps for a deleted blob (`az storage blob undelete`, run by the owner) and for a lost account (not recoverable beyond LRS; recorded as a risk with the GRS option and its cost) are written down | Owner item 6 |
| D10 | **Store-cost corrections** (owner finding). `store_costs` gains `superseded_at`, `superseded_by` (self FK), `change_note`. **Correct** (admin; note required): writes a new row with the corrected amount, other cost, description, lease end or dates and marks the old row superseded; a superseded row counts for nothing (current cost, margin, P&L) and stays in the history list, struck through, with who and why. A correction may move `effective_from` or `effective_to`, but the result may not overlap another live row of the store (refused with a sentence). **Close** (admin; note required): sets `effective_to` on the open row (not before its `effective_from`); later months have no cost row and read "unknown"; to state "no cost from here", staff add a row with landlord rent 0.00, which is a known zero. The open-row and overlap rules, the importer's comparison and the P&L all consider live rows only. Every action audited with before and after | Owner finding of 2026-10-05 |
| D11 | **Report statement timeout and post-import ANALYZE** (binding carry-over). On PostgreSQL the P&L page and export run their queries inside one transaction with `SET LOCAL statement_timeout` = `REPORT_STATEMENT_TIMEOUT_MS` (default 15,000; env guard: 1,000–120,000); a cancelled query answers 200 with "The report took too long. Try a shorter range, or try again in a minute." and logs the class name; the CSV export checks with a first cheap pass before it starts streaming. `import_store_costs` runs `ANALYZE store_costs` after a real run that changed anything (PostgreSQL only). A helper `ledger.services.maintenance.analyze(*models)` is the one place that does it, for Step 13's importer to call | Owner instruction of 2026-10-05; QA-010b addendum |
| D12 | **Invisible-character notes** (deferred MINOR of QA-010): the manual-payment note, the return-resolution note and the link-invoice note (when required) use `visible_text` before the emptiness check, in the form and the service, like the mismatch and waive notes | Deferred finding; small |

## 4. Interface contract

Settings/env: `DOCUMENTS_STORAGE_ACCOUNT_URL` (https URL of the account's blob endpoint; empty =
feature off outside local), `DOCUMENTS_CONTAINER` (default `documents`), `AZURE_CLIENT_ID`
(required when the URL is set), `REPORT_STATEMENT_TIMEOUT_MS`. Package `documents_storage`:
`get_storage()`, `DocumentStorage`, `StorageUnavailable`. Service `ledger/services/documents.py`:
`sniff(data, filename) -> Sniffed | Refusal`, `extract_text(sniffed, data) -> str | Refusal`,
`upload(owner, kind, uploaded_file, note, actor) -> Outcome`, `open_for(document, actor, inline)`,
`delete(document, note, actor)`, `documents_of(owner, include_deleted)`. URLs:
`ledger:documents:upload_store|upload_lease|download|preview|delete`. Models: `Document`
(reshaped), `DocumentKind`; `StoreCost.superseded_at|superseded_by|change_note`. Services:
`store_costs.correct_cost`, `store_costs.close_cost`; `maintenance.analyze`. Bicep: module
`storage.bicep`, outputs `documentsStorageAccountName`, `documentsContainerName`.

## 5. Work breakdown

| Batch | Task | Done when |
|---|---|---|
| A | `documents_storage` package and the import fence test; settings, env guard, upload handlers; `Document` reshape migration; `documents` service (sniff, extract, guard, upload, open, delete); views, forms, templates, drag-and-drop script, create-form upload; registry; audit; dependencies pinned; tests | Uploads, list, preview, download and delete work against the memory storage; every refusal stores nothing |
| B | `infra/modules/storage.bicep`, role assignment, web env wiring in `main.bicep`, `.env.example`; Bicep tests | `az bicep build` clean; what-if shows the account, the container, one role assignment and the web env change only |
| C | D10 store-cost corrections (model, service, store page, importer and P&L on live rows); D11 timeout and ANALYZE; D12 notes; tests | Correct and close work and are audited; a forced slow query answers the message; the importer analyses |
| Docs (Fable) | docs/CLAUDE.md Rule 1 addition (file policy), DATA_OWNERSHIP, RUNBOOK §12 (documents, retention, restore) and §11 update (corrections), ADR-0008, README, `.env.example` review, example files for the owner's clicks (a clean PDF, a PDF with a fake routing-and-account pair), QA-010c | |

A then C touch `ledger` files in sequence (never concurrently); B touches `infra/` only and may
run in parallel with A.

## 6. Test plan (before implementation)

Happy paths: upload each allowed type to a store and to a lease; list shows it; preview streams
PDF/PNG/JPEG inline with the sandbox headers; download streams the same bytes (sha256 equal) as
an attachment with the sanitised name; admin delete hides it from non-admins and keeps the blob;
upload during store and lease creation; correct a cost row (history shows both, P&L uses the new
one); close a cost row; the timeout message; ANALYZE issued after an import that changed rows.
Boundaries: exactly 25 MB accepted, one byte more refused; a zero-byte file refused; a filename
of 300 characters, with path separators, with RTL override and control characters, with only an
extension; `.JPG` upper case; `.jpeg` with JPEG content; a PDF with 0 pages, 500 pages, 501
pages; a scanned PDF (no text) accepted; DOCX with text split across runs so that a card number
is only visible when runs are joined; a routing number and an account number in different
paragraphs (refused when adjacent after normalisation, as the text guard defines) and far apart
(the text guard's rule decides; mirrored exactly); the same file twice on one record; the same
file on two records (allowed). Error paths: extension and content disagree (a PNG named `.pdf`,
a zip named `.docx` without `word/document.xml`, a `.docm`/macro document, an HTML file named
`.pdf`, an EXE named `.png`); encrypted PDF; corrupt PDF and corrupt zip → one sentence each;
storage `put` failing → no row and a sentence; storage read failing on download → a sentence and
an audit row; feature off → section says so and POST refused; correction that would overlap;
close before `effective_from`; correction or close by billing → 403. Adversarial (Fable): a PDF
whose text holds a Luhn-valid card number, a routing/account pair, and the same split across
lines and with spaces and dashes → refused, and afterwards the memory storage holds no blob, the
table no row, and no log line, audit row or page contains the number; a DOCX with the number in a
header, a footnote and a comment; a zip bomb as `.docx` and a pypdf decompression bomb → refused
within the caps, no traceback; a polyglot (PDF header followed by HTML/script) is never served
with a script-capable type and the preview response always carries `sandbox` and `nosniff`; a
filename carrying a card number or `"><script>` is refused or rendered inert; a `Content-Length`
of 30 MB is refused without reading the body; a chunked upload with no length stops at the cap;
a viewer's POST to upload and delete → 403, a no-role user's GET of download → 403, an
unauthenticated GET → the login redirect; download and preview of a document of another record
by guessing its UUID is still role-gated and audited; a deleted document's download by billing
→ 404; path traversal in the document ID → 404; two concurrent uploads of the same file to one
record → one row; the import fence (no Azure SDK import outside `documents_storage`); the SDK is
never constructed with a key or connection string (static check); P&L under a forced
`statement_timeout` of 1 ms answers 200 with the message on both routes; a superseded row never
counts in margin or P&L; history survives a correction of a correction.

## 7. Risk register

- **Rule 1 exposure:** an image or a scanned PDF can show bank details and cannot be checked.
  Mitigation is policy and the notice (owner's item 5), plus admin delete. Recorded in
  docs/CLAUDE.md as the stated limit.
- **Malware:** no virus scanning (no service chosen). Files are only stored and streamed back
  with safe headers, never executed or converted server-side beyond text extraction. Known gap;
  Microsoft Defender for Storage is the option, at a cost, for the owner to decide.
- **Memory:** a 25 MB upload is held in memory (twice briefly during hashing and extraction);
  web replicas have 1 GiB and four threads. Four simultaneous maximum uploads fit; more queue at
  gunicorn. pypdf on a hostile file can be slow: page cap, size cap, and the request timeout
  bound it. Tested with the caps.
- **Stored-content XSS:** preview is the only inline route; sandbox CSP, nosniff, sniffed type,
  and only PDF/PNG/JPEG are ever inline.
- **Identity and network:** access is by managed identity with Entra-only auth and no shared
  keys; the endpoint is public. A model never mints or reads a token; the SDK does at runtime.
  Role assignment needs a few minutes to propagate after the deployment.
- **Irreversible:** dropping the reserved `operator` and `file_reference` columns (guarded: the
  migration refuses when a row exists; the table has never had a writer).
- **Shared code:** `store_costs` live-row rule touches the importer, the store page and the P&L;
  the upload handler setting is global (no other upload exists in the app; verified in QA).
- **New dependencies:** `azure-storage-blob`, `azure-identity`, `pypdf`: pinned, licences
  MIT/MIT/BSD-3, transitive packages listed in the QA report.

## 8. Acceptance criteria

Gate green on both engines; migrations clean; `az bicep build` clean and the what-if limited to
D1's resources and the web env; no Azure SDK import outside `documents_storage`; every refusal
path leaves no blob and no row; on staging after the infrastructure deployment: a PDF lease on a
lease and a floor-plan image on a store appear in their lists, preview and download work, a
viewer sees but cannot upload, the PDF with a fake routing-and-account pair is refused, a 30 MB
file is refused, and the audit log shows the owner's upload and download; a store cost row can be
corrected and closed by an admin with history kept.

## 9. Amendments during QA (2026-10-05)

- **D6 widened.** The guard text of a PDF also includes every form field's name, value, default
  and options, every annotation's contents, the document information dictionary and XMP; of a
  DOCX, the text of every element of every XML part, document properties, alt text, tag and
  alias values and relationship targets. Pieces other than page or paragraph text are separated
  by four line breaks so that neighbouring properties do not merge into one digit run. A PDF or
  DOCX that carries an embedded file is refused (`embedded_object`).
- **D7 preview headers.** A PDF preview carries `default-src 'none'; frame-ancestors 'self'`
  without `sandbox` (the browser's PDF viewer does not run in a sandboxed frame); PNG and JPEG
  previews and every download keep `sandbox`.
- **D3 request limit.** The 26 MiB request limit applies only to the two upload routes and the
  store and lease create forms; every other route keeps 2.5 MiB.
- **D10 addition.** After a closed cost row, a new row or an import must start after its end.
