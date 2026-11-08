"""The invoice and receipt PDFs (docs/qa/15/PLAN-015c-invoice-pdfs.md; ADR-0020): rendered on
request from stored data, never stored. :mod:`.frame` draws the page every document shares,
:mod:`.invoice` and :mod:`.receipt` its body, :mod:`.logo` keeps the logo's PNG copy the
renderer reads."""
