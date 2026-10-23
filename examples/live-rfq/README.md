# Observed live-model runs

These are synthetic RFQs processed by real models through OpenRouter on October 23, 2026. They are separate from the scripted offline replay under `examples/synthetic-rfq/`.

| Scenario | Run ID | Review | Structured audit | Model metadata |
|---|---|---|---|---|
| Sufficient inventory | `2543a0b637e7` | [Quote proposal](standard/proposal.md) | [run.json](standard/run.json) | [Summary](standard/model_summary.json) |
| Stock shortage | `8e63580a8f76` | [Blocked proposal](shortage/proposal.md) | [run.json](shortage/run.json) | [Summary](shortage/model_summary.json) |

Both used `deepseek/deepseek-v4-flash-0731` for classification and `deepseek/deepseek-v4-pro-0813` for research and extraction. Each made five model requests and ten local read-only tool calls, followed by nine independent Python validation lookups.

The source email, prices and tax rates were identical. Dispenser inventory changed from 12 to 2; the request stayed at 3. Python returned `quote_ready` with a $90.52 total in the first case, and `insufficient_stock` with no aggregate totals in the second. Both kept email and Teams delivery disabled.

`run.json` and `proposal.md` are copies of the actual synthetic run artifacts. The metadata summary selects returned model IDs and call types from the recorded provider responses; it omits raw provider messages and private operational history. Its SHA-256 ties the summary to the corresponding audit file. No credential file or private run directory is included.

Earlier incomplete extractions were blocked and retained locally, and an initial shortage request encountered a temporary provider rate limit before the completed run. These two completed examples demonstrate expected behavior after protocol corrections; they are not a model-accuracy benchmark. See the [case study](../../docs/CASE_STUDY.md) and [live-run setup](../../docs/LIVE_DEMO.md).
