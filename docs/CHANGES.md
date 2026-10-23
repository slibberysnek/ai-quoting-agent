# Portfolio preparation changes

The original preparation pass prioritized a small credible quoting demonstration and stayed local. A subsequent user request authorized GitHub source publication.

- Reframed the README around the business workflow and linked a concise case study, architecture diagram, status inventory and validation report.
- Added a synthetic Cedar Workshop RFQ, minimal catalog/customer data, approved conversions and explicitly authored model-response fixtures. `python -m quote_agent.demo` runs the real pipeline without accounts or network. Standard, shortage and malformed-model scenarios stop at human review.
- Added stable standard and shortage proposal/audit samples. Agent research and independent deterministic lookups now have separate traces. Validator failures retain the extracted draft and safe lookup-error details.
- Fixed stock validation across repeated product lines; rejected invalid normalized quantities, invalid prices/taxes, mismatched item/unit data, inconsistent repeated prices and invalid inventory. Existing pricing-agreement review remains explicit.
- Made failed or truncated attachment extraction and unverified requested dates block ready quotes. Tightened quantity/ID schemas and nonfinite-value rejection. Final replies continue to ignore model-proposed financial prose.
- Added 37 portable regression tests for these boundaries and external failure behavior; legacy data-dependent tests now skip explicitly in a checkout without the ignored fixtures.
- Made the SQL Server driver an optional cloud dependency; packaged all demo assets, added a tested dependency snapshot and placeholder environment example, and fixed the live CLI reference-directory default to use packaged CSVs.
- Replaced account-specific deployment instructions with a generic adapter configuration contract. Extended ignore rules for environment variants, private keys and build output. The existing local key and private runs were retained and excluded, without exposing their values.
- Preserved the original converter documentation in `docs/WWI_SQLITE.md`, documented legacy fixture prerequisites, and labeled older hosting/workflow plans as historical. Fixed the fixture verifier's rejection of Finder metadata and an obsolete comment.
- Verified both existing WWI databases and the broader document pack. The converter itself was left intact; the larger pack was not regenerated.

**Evidence:** 74 tests pass with existing data. In a fresh dependency environment without WWI data, 56 pass and 18 legacy tests skip. The installed wheel also runs the synthetic demo. See [EVALUATION.md](EVALUATION.md) for limits and commands.

## Subsequent live synthetic demonstration

Added `python -m quote_agent.live_demo --key-file ./or_key.txt` with an optional trusted CA bundle. It reads the explicitly selected local key, rebuilds only synthetic business data, blocks all network destinations except OpenRouter completions, blocks redirects, and initializes no delivery adapters. Actual model outputs and local review artifacts are retained in unique ignored run directories.

Live attempts exposed missing final extraction fields. The handler now separates research from finalization, and the API wire schema requires explicit fields while preserving local Pydantic defaults. Two blocked attempts were retained; the corrected live path produced a Python-validated $90.52 proposal. Added four regression tests; all 78 tests pass with existing data, and 60 pass with 18 optional skips without that data. The Microsoft integrations remain unverified. No email, Teams post, commit, push or publication occurred.

## GitHub portfolio packaging

Rewrote the README around the business problem, AI/Python trust boundary, two observed live outcomes and reproducible setup. Added selected synthetic live proposal/audit snapshots with model metadata summaries, plus a no-secrets GitHub Actions workflow for the portable suite. Private credentials, raw runs, databases, deployment ZIPs and exploratory planning notes remain local and ignored. Source publication does not deploy the application or enable delivery integrations.
