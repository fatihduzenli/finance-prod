# The owner's inputs live outside this repository

The audits of the owner's Google Sheet, the owner's written answers (rounds 1 and 2), the prompt
series amendments, and the real store-cost rows that once sat here and in `docs/templates/` hold
business data: store numbers with their towns,
monthly figures and totals, operator nicknames and staff names. Since Step 13a (2026-10-27) they
are kept in the private Blob container **`owner-inputs`** of the staging documents storage
account in resource group `finance-sandbox`, under `inputs/<date>/`, readable by the owner's
account alone (one Storage Blob Data Contributor role assignment; public access is off at the
account). Nothing in that container is needed to build, test or deploy the app.

Documents in this folder's history and in the docs that cited them keep placeholders instead of
the values ("the three staff members", "two twice-monthly stores", "the August 2026 figures"). The
Google Sheet itself was never in the repository. Removing a file from the working tree does not
remove it from the repository's history; the owner decides on any history rewrite apart from this
step.

The two Stax notes stay here (`STAX_ENDPOINTS_2026-09-30.md`, which a test reads, and
`STAX_ANSWERS_public_2026-09-10.md`): they hold the processor's public documentation and support
answers, no business data.
