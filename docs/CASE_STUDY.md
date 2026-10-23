# AI-assisted quoting with deterministic business rules

A wholesale quote starts with a deceptively simple email: a customer asks for several products in different quantities and units. Preparing a useful reply requires interpreting the request, identifying the right catalog items, checking stock, applying approved prices, calculating tax, and spotting anything that needs clarification. The commercial opportunity is to organize that work into a reviewable proposal while keeping financial decisions under explicit business rules.

I built an independent Python prototype around that workflow. Its output is a proposed quote or a clearly routed exception, supported by the original email, extracted requirements, catalog facts, and an execution record. It does not send customer emails.

## Proposed automation

The workflow parses an RFQ and its supported attachments, classifies the message, and uses a second model to extract requirements and research relevant facts through read-only tools. A Python pipeline then resolves products, normalizes units, checks inventory, calculates catalog pricing and tax, and prepares a response for a person to review.

Unclear products, missing information, stock shortages, special pricing references, malformed model responses and dependency failures produce explicit outcomes. The goal is a dependable handoff to a reviewer, not autonomous sales authority.

## Architecture

The central design decision is separating **language interpretation from business authority**. AI handles the flexible interface: customer language, product descriptions, intent and tool selection. Deterministic code handles the repeatable rules: approved unit conversions, inventory limits, prices, tax arithmetic and outcome selection.

The model returns a typed draft without price or total fields. The validator performs its own data lookups rather than trusting the model's research trace. It uses Decimal arithmetic with explicit rounding, and the final response is rendered from validated results. This makes financial behavior inspectable and testable independently of a model provider. The [architecture diagram](ARCHITECTURE.md) shows the boundaries.

Microsoft Graph, Azure SQL and Teams adapters extend the same design toward mailbox ingestion, business-data access and review delivery. They are implemented and tested with fake transports or connections. Their current live operation remains unverified.

## Trust boundaries

Email and attachments are untrusted input. Structured schemas reject unexpected financial fields and invalid quantities; the model can call only registered read tools. Local database access is read-only and SQL is fixed and parameterized. An explicit SKU hint must also appear in the source. Similarity search alone cannot authorize a catalog match.

The validator checks combined demand across repeated product lines, approved conversions, inventory, item/unit consistency, valid catalog prices and tax rates. It withholds aggregate totals when a request is blocked. Pricing agreements require human review; the model cannot apply a discount by writing one into its suggested reply.

These controls do not make extraction infallible. A model can still omit a line or extract an incorrect but valid quantity. Human review must compare the proposal with the original request. The application has no approval state machine or customer-send endpoint.

## Demonstration

The demonstration uses invented Cedar Workshop data. The RFQ requests two outers of packing paper and three tape dispensers, with freight and delivery to be confirmed. An offline replay uses authored model responses for reproducibility; the parser, agent protocol, local tools, validator and renderer execute normally. [Run the walkthrough](../examples/synthetic-rfq/README.md).

I also ran both scenarios through real DeepSeek Flash and Pro models via OpenRouter. The customer email, catalog prices, tax rates and unit-conversion rules stayed the same. Only dispenser inventory changed:

| Real-model run | Dispensers requested / available | Python outcome | Quote-level financials |
|---|---|---|---|
| [Successful quote — `2543a0b637e7`](../examples/live-rfq/standard/run.json) | 3 / 12 | `quote_ready` | Subtotal **$84.20**, synthetic tax **$6.32**, total **$90.52 before freight** |
| [Stock shortage — `8e63580a8f76`](../examples/live-rfq/shortage/run.json) | 3 / 2 | `insufficient_stock` | Subtotal, tax total and overall total **withheld (`null`)** |

In both runs, the models correctly extracted the original two outers and three individual dispensers and made ten read-only tool calls. Python then performed nine independent validation lookups. It converted the paper to 20 units using the approved rule; 80 paper units were available in both scenarios. Each run saved a proposal, structured audit and actual model-response metadata locally. [Live-run instructions](LIVE_DEMO.md) describe the setup and safety boundaries.

**The blocked quote demonstrates successful enforcement of the inventory rule.** The RFQ was understood correctly, but three requested dispensers exceeded the two available. Python detected that business condition, selected `insufficient_stock`, and withheld the aggregate financial figures. The request remained at three units, with the shortage clearly identified for a human to resolve. The generated reply explained that the full quantity was unavailable.

This outcome shows the intended trust boundary in operation: the model supplies interpreted requirements, while deterministic code decides whether a quote can proceed. A stock shortage is a handled business exception. Line-level catalog facts remain available for review, but the system does not present a completed quote total.

Both live scenarios kept all business data synthetic. Only OpenRouter received network requests; no email or Teams delivery adapter was initialized, and neither proposal was sent. Earlier live attempts that omitted extracted lines were also blocked and retained; they led to separate research/finalization calls and explicit required fields in the API schema.

## Evaluation

The regression suite checks the behavior that matters at the business boundary: missing data, ambiguous or unknown products, invalid quantities, stock shortages including repeated lines, invalid or inconsistent catalog data, malformed responses, restricted tools, model-proposed financial overrides, and failed integrations. A fresh checkout can run the portable suite without the large WWI data files; optional historical checks use the existing eight RFQs and sample databases.

The [evaluation report](EVALUATION.md) records test commands, pass/skip counts, environment and limits. The two observed live runs demonstrate the expected quote and shortage paths with real model outputs. They are examples of end-to-end behavior, not a controlled accuracy benchmark. Neither the tests nor these examples establish measured labor savings or production reliability.

## Limitations and next step

The project is a local demonstrator with a verified live OpenRouter path for these synthetic examples and mock-tested Microsoft cloud adapters. It has no verified production deployment, durable cloud job queue, retry store, cross-request deduplication, stock reservation, or customer-email delivery. Tax is a catalog fixture rule; freight and delivery commitments require review. The live synthetic runner contacts OpenRouter only and keeps email and Teams delivery disabled. The separate Microsoft webhook path can automatically post a Teams review, but its live operation remains unverified.

The next evidence needed is a labeled set of representative RFQs evaluated against live model outputs, followed by an isolated test of the cloud chain. Any commercial pilot would also need durable processing, explicit approval records, and agreed pricing and inventory policies. No time savings, revenue improvement, customer adoption or production use is claimed.
