# Historical WWI data and fixtures

The original converter and larger document pack remain available, separately from the small portfolio replay. They are useful historical work and are not required for the portable demonstration.

## Existing outputs

`data/sqlite/` holds local OLTP and DW databases. `data/portfolio_artifacts/` holds 78 generated artifacts plus a checksum manifest: margin inputs, eight RFQs with attachments, supplier invoices, delay correspondence and receivables cases. Only the quoting workflow is implemented; the other scenario inputs do not prove corresponding automation exists.

Both directories are ignored. A new checkout will not contain them. Existing legacy tests skip explicitly when the database or RFQ pack is missing. Portable tests in `test_synthetic_rfq.py` always run and use the checked-in synthetic data.

## Verification

With the historical data present:

```sh
python -m wwi_sqlite verify --output-dir data/sqlite
python tools/verify_portfolio_fixtures.py
python -m unittest discover -s tests -v
```

The fixture verifier ignores Finder `.DS_Store` metadata but still requires every artifact to match its manifest. [WWI_SQLITE.md](WWI_SQLITE.md) documents converter build and offline rebuild commands. The converter is standard-library Python.

## Optional regeneration

The historical fixture generator needs the two WWI databases, Python document-generation dependencies, and a Node environment containing `@oai/artifact-tool`. That package is an environment-specific prerequisite; the repository does not supply a portable Node lockfile. Full historical fixture regeneration was not performed in this audit.

In such an environment, the existing command requires **both** output and QA arguments:

```sh
python -m pip install -e '.[fixtures]'
node tools/generate_portfolio_spreadsheets.mjs data/portfolio_artifacts data/portfolio_qa
python tools/generate_portfolio_fixtures.py
python tools/verify_portfolio_fixtures.py
```

The historical live-model CLI can then process `data/portfolio_artifacts/02_inbound_quote/emails/rfq_001_exact.eml` using the OLTP database and packaged WWI reference CSVs. It requires an OpenRouter key and supported model IDs. No current model availability or accuracy was verified. Use synthetic requests and approved service configuration when testing.
