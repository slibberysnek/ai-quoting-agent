# Implementation status

Inspected and validated October 23, 2026. Evidence is from source, local tests, fixture verification and private historical run summaries. The initial audit was offline. A subsequent live OpenRouter check used only synthetic business data; Microsoft service connectivity remains unverified.

| Area | Status | Evidence and boundary |
|---|---|---|
| Synthetic RFQ replay | Working locally | Rebuilds a tiny SQLite fixture; real parser, agent protocol, read tools, validator and renderer; model replies are authored |
| Email and attachments | Working locally, partially covered | Eight existing RFQs parse, including XLSX attachments; CSV/text/XLSX/PDF/DOCX extraction implemented; malformed/truncated extraction blocks quotes; no OCR |
| Catalog and financial validation | Working locally | Schema checks, curated matching/conversions, stock checks including repeated lines, catalog/tax validation, Decimal rounding, manual pricing review |
| Review artifacts | Working locally | Markdown proposal plus JSON draft, agent research and independent validation traces; no durable cloud case store |
| Live OpenRouter interpretation | Verified on standard and shortage scenarios | Each completed run used five real model calls, ten local read-only tools and nine independent validation lookups. The standard run produced $90.52; the shortage run withheld totals because 3 dispensers exceeded 2 in stock. [Public synthetic audit snapshots](../examples/live-rfq/README.md) are included; earlier blocked attempts and raw provider responses remain local. This is not an accuracy benchmark. |
| Microsoft Graph | Mock-tested adapter; live unverified | Token form, encoded MIME request, subscription creation, validation-token handshake and notification clientState checks; renewal branch implemented but not separately covered |
| Azure SQL | Mock-tested adapter; live unverified | DW catalog mapping and parameterized customer lookup tested using fake connections; real driver, credentials and SQL schema connectivity unverified |
| Microsoft Teams | Mock-tested adapter; live unverified | Text rendering, POST body and network/HTTP failure handling tested with fake transport; live Workflow delivery unverified |
| Web coordination | Working in local TestClient; live unverified | Health, notification validation, same-batch deduplication, acknowledgement and logged background failures |
| WWI converter | Existing local outputs verified | 48 OLTP tables / 4,713,833 rows and 29 DW tables / 923,643 rows; integrity, row counts, FK and representative-type checks pass; no rebuild performed |
| Broader document fixture pack | Existing generated artifacts verified | 78 artifacts plus manifest; checksum, attachment, source-ID and margin anchors pass. These are test inputs, not four other implemented agents |
| Historical planning | Conceptual | Other automation workflows, conversational revisions, alternative hosting plans and commercial pilot ideas remain proposals. Exploratory planning notes are retained locally and excluded from the public source |

There is no verified end-to-end Microsoft cloud integration, production deployment, customer adoption, measured time saving, revenue claim, or measured model accuracy in this portfolio package.

## Incomplete work that matters before deployment

There is no durable queue or retry mechanism, cross-request idempotency, cloud persistence, automated Graph renewal, authenticated approval state machine, Outlook draft creation, customer reply sending, or inventory reservation. Background-task failures after HTTP 202 are logged and can lose work. Teams delivery occurs automatically on the live path, before human review.

Catalog tax is a sample rule, not jurisdiction-aware calculation. Pricing agreements are flagged and never automatically applied. Currency is displayed as dollars without multi-currency support. Freight, delivery commitments, semantic extraction correctness, and customer identity remain reviewer responsibilities. Classification confidence is displayed but has no calibrated threshold or measured meaning.

## Publication hygiene

The repository initially had no commits and all source files were untracked. No Git history was rewritten. Account-specific deployment examples were replaced with placeholders. The local OpenRouter key, ignored private runs, databases, generated document pack and deployment ZIP remain local and excluded by `.gitignore`. A targeted scan found an API-key pattern only in the ignored key file; this is not a full forensic secret audit. The published live examples are explicitly selected synthetic proposal/audit snapshots; credentials and raw private run history are excluded.
