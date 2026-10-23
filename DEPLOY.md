# Optional cloud integration setup

**Implemented adapters; current live deployment unverified.** Use the [offline replay](README.md#run-the-demonstration) for portfolio review. This document describes the application's configuration contract, not a proven hosting recipe.

## Local web shell

```sh
python -m pip install -e '.[dev]'
quote-agent-web
```

`GET /health` returns `{"status":"ok","service":"obelyr-quote-agent"}` without initializing Graph, SQL, Teams, or the model client. A healthy response proves process availability only. The ASGI entry point is `quote_agent.web:app`.

## Live adapter configuration

Install the optional driver only where needed:

```sh
python -m pip install -e '.[cloud]'
```

`requirements.txt` retains the complete web/cloud dependency list for environments that install that file. Driver and host compatibility must be checked on the intended platform; cloud installation was not verified in this pass.

Set the following process environment variables. [.env.example](.env.example) contains placeholders; the application does not load it automatically.

| Variable | Purpose |
|---|---|
| `MICROSOFT_TENANT_ID`, `MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET` | Application client credentials used by `GraphClient` |
| `MICROSOFT_MAILBOX` | Test mailbox whose MIME messages the app may read |
| `GRAPH_CLIENT_STATE` | Shared secret checked on webhook notifications |
| `OPENROUTER_API_KEY` | Model service credential |
| `AZURE_SQL_CONNECTION_STRING` | Read-only SQL connection to a WWI DW-compatible schema |
| `TEAMS_WEBHOOK_URL` | Secret HTTPS review-delivery URL accepting `{"text":"..."}` |
| `PORT` | Web listener port; default 8000 |

Application permissions, tenant consent, mailbox scope, public HTTPS hosting, SQL network access and least-privilege grants are external prerequisites. They have not been verified here. No existing account or service resource name is needed for the offline demo.

## Implemented path

Graph validation tokens are echoed as plain text. Notifications must include the configured `clientState`; accepted message IDs are queued in FastAPI background tasks. The app downloads MIME, runs the pipeline against Azure SQL, and **automatically posts the proposal to Teams for review**. Customer email sending is absent.

Thus the live adapter does make an external review post before a human reads it. The offline demo makes no external action at all. Neither path has an approval button or approval state machine.

`AzureSqlQuoteTools` reads `[Dimension].[Stock Item]`, `[Fact].[Stock Holding]`, and `[Dimension].[Customer]`. The DW lacks sender email identity; customer resolution uses a name hint. Product aliases, unit conversions, and agreement references come from the packaged CSVs. Catalog data is cached for up to 60 seconds; it is not a stock reservation.

After deliberately configuring a dedicated test deployment, the existing subscription helper can create or renew its matching subscription:

```sh
python -m quote_agent.subscribe --notification-url 'https://<test-host>/webhooks/graph'
```

This command changes external subscription state; it is not needed for the portfolio demo. The implementation requests a six-day lifetime. Renewal behavior is a code contract, not a statement of current provider limits.

## Operational limitations

There is no durable queue, cross-request deduplication, retry store, persisted cloud case history, automated subscription-renewal scheduler, Outlook draft creation, or customer send endpoint. Duplicate IDs are suppressed within one notification batch only. A background failure is logged after HTTP 202; an acknowledgement does not prove processing or Teams delivery succeeded. Logs and model inputs may contain sensitive business data and need an appropriate deployment policy.

Historical deployment ZIPs, local secrets, and private run records remain ignored. The earlier account-specific deployment instructions were replaced with this generic configuration contract. No deployment, subscription change, customer message, push, commit, or publication was performed during portfolio preparation.
