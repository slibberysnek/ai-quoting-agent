"""Render one run as a reviewer-friendly Markdown proposal."""

from __future__ import annotations

import json

from .models import RunRecord


def _cell(value: object) -> str:
    return str(value if value is not None else "—").replace("|", "\\|").replace("\n", " ")


def _money(value: float | None) -> str:
    return "—" if value is None else f"${value:,.2f}"


def _blockquote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


def render_markdown(run: RunRecord) -> str:
    """Create the human-review artifact that a future Teams card can summarize."""

    email = run.email
    result = run.result
    sections = [
        f"# Quote review: {result.outcome.value}",
        "",
        f"- **Run:** `{run.run_id}`",
        f"- **From:** {_cell(email.sender)} <{_cell(email.from_address)}>",
        f"- **Subject:** {_cell(email.subject)}",
        f"- **Classification:** `{run.classification.intent.value}` "
        f"({run.classification.confidence:.0%})",
        f"- **Customer:** {_cell(result.customer.customer_name)}",
        f"- **Reason codes:** {', '.join(f'`{code}`' for code in result.reason_codes) or 'none'}",
        "",
        "**Human review required. This proposal has not been sent.**",
        "",
        "## Decision",
        "",
        run.classification.rationale,
    ]
    if result.issues:
        sections.extend(["", *[f"- {issue}" for issue in result.issues]])
    if result.pricing_agreements:
        sections.extend(["", "## Pricing agreements", ""])
        for agreement in result.pricing_agreements:
            sections.append(
                f"- `{agreement.agreement_id}` — {agreement.discount_percentage:g}% reference "
                f"rate, active {agreement.start_date} through {agreement.end_date}; manual review required"
            )

    sections.extend(
        [
            "",
            "## Quote lines",
            "",
            "| Requested | Match | Stock | Unit price | Subtotal | Tax | Line total |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    if result.lines:
        for line in result.lines:
            requested = f"{line.requested_quantity:g} {line.requested_unit} {line.requested_description}"
            match = (
                f"#{line.stock_item_id} {line.stock_item_name} ({line.match_type})"
                if line.stock_item_id
                else "Unresolved"
            )
            stock = (
                f"{line.quantity_on_hand} {line.normalized_unit}"
                if line.quantity_on_hand is not None
                else "—"
            )
            sections.append(
                "| "
                + " | ".join(
                    [
                        _cell(requested),
                        _cell(match),
                        _cell(stock),
                        _money(line.catalog_unit_price),
                        _money(line.line_subtotal),
                        _money(line.tax_amount),
                        _money(line.line_total),
                    ]
                )
                + " |"
            )
    else:
        sections.append("| No line items extracted | — | — | — | — | — | — |")

    sections.extend(
        [
            "",
            f"**Subtotal:** {_money(result.subtotal)}  ",
            f"**Tax:** {_money(result.tax_total)}  ",
            f"**Total:** {_money(result.total)}  ",
            f"**Freight:** {result.freight}",
            "",
            "## Proposed reply",
            "",
            f"**Subject:** {result.proposed_subject}",
            "",
            _blockquote(result.proposed_body),
            "",
            "## Original email",
            "",
            _blockquote(email.body),
        ]
    )
    if email.attachments:
        sections.extend(["", "## Attachments", ""])
        for attachment in email.attachments:
            sections.append(
                f"### {attachment.filename}\n\n"
                f"Type: `{attachment.content_type}` · SHA-256: `{attachment.sha256}`"
            )
            if attachment.extraction_error:
                sections.append(f"\nExtraction issue: {attachment.extraction_error}")
            elif attachment.extracted_text:
                sections.append("\n" + _blockquote(attachment.extracted_text))
    if run.tool_trace:
        sections.extend(["", "## Agent tool trace", ""])
        for index, trace in enumerate(run.tool_trace, start=1):
            details = json.dumps(
                {"arguments": trace.arguments, "result": trace.result},
                ensure_ascii=False,
                indent=2,
            )
            sections.extend([f"### {index}. `{trace.name}`", "", "```json", details, "```"])
    if run.validation_trace:
        sections.extend(["", "## Deterministic validation trace", "",
                         "Fresh lookups used for the final decision; separate from agent research.", "",
                         "```json", json.dumps([t.model_dump() for t in run.validation_trace], indent=2), "```"])
    return "\n".join(sections).rstrip() + "\n"
