# Synthetic RFQ walkthrough

This replay uses invented Cedar Workshop data and `.example` addresses. `quote_agent/demo_data/model_messages.json` contains **authored** classification, tool-selection and extraction responses. They are not recorded live model output.

1. Read [the incoming email](../../quote_agent/demo_data/incoming.eml). It requests 2 outers of packing paper and 3 tape dispensers, with freight to be confirmed.
2. Inspect [the synthetic catalog](../../quote_agent/demo_data/catalog.json) and [model response fixture](../../quote_agent/demo_data/model_messages.json). The classifier labels a new quote; the handler returns typed descriptions, quantities, units, SKU hints, and evidence.
3. Run `python -m quote_agent.demo`. The real agent loop executes nine read-only tool calls. The Python validator independently makes nine further lookups; the JSON audit separates the two traces.
4. Python converts paper to 20 Each using the approved CSV rule. Stock is 80 paper units and 12 dispensers. It calculates $47.00 + $37.20 = $84.20, rounds line taxes to $3.53 and $2.79, and returns **$90.52 before freight**. The rate is synthetic.
5. Inspect the checked-in [proposal](output/proposal.md) and [audit](output/run.json), or the regenerated files under `runs/synthetic-demo/standard/`. The demo uses a fixed timestamp and run ID for exact comparisons.
6. Run `python -m quote_agent.demo --scenario shortage`. Only dispenser inventory changes, to 2 units. The identical request now returns `insufficient_stock` and no aggregate quote total. See the checked-in [blocked proposal](shortage/proposal.md).
7. Run `python -m quote_agent.demo --scenario malformed-model`. The handler's final JSON is malformed. Validation returns `processing_error`; no quote total is emitted. See the tests for additional missing-data, invalid-quantity, ambiguous-product, catalog inconsistency and network-failure scenarios.

All paths stop at a local proposal for a human to review against the source. No Graph read, Azure SQL access, Teams post, or email send occurs. A successful exit from a failure scenario means the expected safe outcome was observed.

To regenerate the checked-in samples deliberately:

```sh
python -m quote_agent.demo --output-dir examples/synthetic-rfq/output
python -m quote_agent.demo --scenario shortage --output-dir examples/synthetic-rfq/shortage
```
