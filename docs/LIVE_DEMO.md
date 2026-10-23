# Live AI with synthetic business data

This command uses real OpenRouter inference for classification, tool selection and extraction. It rebuilds only the small synthetic Cedar Workshop database, then runs the same deterministic validator and proposal renderer as the offline demonstration.

From the project root, after the README's environment setup:

```sh
python -m quote_agent.live_demo --key-file ./or_key.txt
```

`--key-file` explicitly selects the local file and takes precedence over an environment key for this command. The file is read at runtime; it is not copied into requests or artifacts. Only its validated credential is used in the API authorization header. `or_key.txt` remains ignored.

Some local Python installations lack a usable certificate root bundle. If certificate verification fails, locate the installed bundle with `python -m certifi`, then pass that trusted file explicitly:

```sh
python -m quote_agent.live_demo --key-file ./or_key.txt \
  --ca-bundle /path/to/trusted/cacert.pem
```

TLS verification stays enabled. Model requests require OpenRouter credits. The default models are the existing DeepSeek Flash classifier and DeepSeek Pro handler; `--classifier-model` and `--handler-model` can override them.

## Boundaries

- Customer, contact address, product catalog, prices, tax, inventory and approved unit conversions remain synthetic.
- The model receives the synthetic email and facts returned by local read-only tools.
- The runner permits only POST requests to `https://openrouter.ai/api/v1/chat/completions` and rejects redirects. Other urllib endpoints, including Graph and Teams, are blocked before network access.
- No Graph, Azure SQL or Teams delivery adapter is initialized. No email-sending or publishing tool is registered.
- All output is local. Human review is still required; there is no send or approval action in this command.
- Model responses are not forced to agree with the scripted answer. Missing lines, malformed output and other issues remain visible and can block the quote.

Each attempt gets a unique directory under `runs/live-synthetic/` containing:

| File | Evidence |
|---|---|
| `proposal.md` | Clearly labeled live-model proposal awaiting human review |
| `run.json` | Parsed synthetic input, actual classification/extraction, model tool calls, independent validation and result |
| `model_calls.json` | Actual returned model IDs, response messages, provider usage, timings and errors; no request headers or key |

Private run directories are ignored. The observed live examples do not establish extraction accuracy or a verified Microsoft cloud workflow.

## Protocol fixes discovered during the first live attempts

The first attempt classified correctly but returned a partial progress summary with no line items. The validator returned `needs_clarification` with no totals. A second attempt performed ten read-only tool calls but again omitted final line items. Both blocked records were retained.

The handler now separates tool research from final structured extraction. The API schema explicitly requires every field, including nullable fields and the line-item array, instead of allowing provider omissions to take local defaults. The finalization request also supplies the schema in its instructions. Local application defaults remain available, and all financial validation rules remain unchanged.

The regression suite also checks the network allowlist, redirect blocking, explicit key-file selection, partial-response finalization and required wire-schema fields without contacting a model.

## Observed live runs

On October 23, 2026, run `2543a0b637e7` returned `quote_ready` after five live model calls. The returned models were `deepseek/deepseek-v4-flash-0731` and `deepseek/deepseek-v4-pro-0813`. Ten model-selected read-only tool calls and nine independent validator lookups produced subtotal $84.20, synthetic tax $6.32, and total $90.52 before freight. Actual results are local under `runs/live-synthetic/2543a0b637e7/`.

Run `8e63580a8f76` processed the same RFQ with only dispenser stock reduced to 2. It correctly retained the request for 3 dispensers and returned `insufficient_stock`, with subtotal, tax total and total all null. It also used five model calls, ten read-only tools and nine independent validation lookups. The initial rate-limited attempt was retained locally.

The [public synthetic snapshots](../examples/live-rfq/README.md) include both completed proposals and structured audits. Earlier incomplete extractions were blocked and retained locally. These are observed examples after protocol corrections, not a benchmark. With the new regressions, 78 tests pass when optional legacy data is present; without that data, 60 pass and 18 skip explicitly.
