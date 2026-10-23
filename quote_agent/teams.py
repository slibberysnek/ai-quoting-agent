"""Post human-reviewable quote summaries to a Microsoft Teams Workflow.

The first cloud version uses the ordinary Teams "Send webhook alerts to a
channel" workflow.  That keeps Teams as a notification/review surface without
introducing a bot registration before conversational actions are actually
needed.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from .models import RunRecord


MAX_TEAMS_TEXT_CHARS = 20_000


class TeamsWebhookError(RuntimeError):
    """Raised when a quote review cannot be delivered to Teams."""


def _money(value: float | None) -> str:
    """Format optional quote totals without inventing a missing amount."""

    return f"${value:,.2f}" if value is not None else "TBD"


def render_teams_message(run: RunRecord) -> str:
    """Render one compact Markdown message for the Quotes channel."""

    result = run.result
    email = run.email
    lines = [
        f"## Quote review: {result.outcome.value.replace('_', ' ').title()}",
        f"**From:** {email.sender} <{email.from_address}>",
        f"**Subject:** {email.subject}",
        (
            f"**Classification:** {run.classification.intent.value.replace('_', ' ')} "
            f"({run.classification.confidence:.0%} confidence)"
        ),
        f"**Run ID:** `{run.run_id}`",
    ]

    if result.lines:
        lines.extend(["", "### Quote lines"])
        for index, item in enumerate(result.lines, start=1):
            product = item.stock_item_name or item.requested_description
            requested = f"{item.requested_quantity:g} {item.requested_unit}"
            details = [f"requested {requested}"]
            if item.quantity_on_hand is not None:
                details.append(f"stock {item.quantity_on_hand:,}")
            if item.line_total is not None:
                details.append(f"line total {_money(item.line_total)}")
            if item.issues:
                details.append("issues: " + "; ".join(item.issues))
            lines.append(f"{index}. **{product}** — " + " · ".join(details))

    if result.subtotal is not None or result.total is not None:
        lines.extend(
            [
                "",
                "### Totals",
                f"**Subtotal:** {_money(result.subtotal)}  ",
                f"**Tax:** {_money(result.tax_total)}  ",
                f"**Total:** {_money(result.total)}  ",
                f"**Freight:** {result.freight}",
            ]
        )

    if result.issues:
        lines.extend(["", "### Needs attention"])
        lines.extend(f"- {issue}" for issue in result.issues)

    lines.extend(
        [
            "",
            "### Suggested email response",
            f"**{result.proposed_subject}**",
            result.proposed_body,
            "",
            "### Original email",
            email.body[:2_500] or "_(No plain-text body was extracted.)_",
        ]
    )

    if email.attachments:
        attachment_names = ", ".join(item.filename for item in email.attachments)
        lines.extend(["", f"**Attachments:** {attachment_names}"])

    rendered = "\n".join(lines)
    if len(rendered) > MAX_TEAMS_TEXT_CHARS:
        rendered = rendered[: MAX_TEAMS_TEXT_CHARS - 80].rstrip()
        rendered += "\n\n_(Message shortened to fit the Teams webhook limit.)_"
    return rendered


class TeamsWebhookClient:
    """Send quote reviews through a channel-scoped Teams Workflow URL."""

    def __init__(
        self,
        webhook_url: str,
        *,
        timeout_seconds: float = 30.0,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        if not webhook_url.strip().lower().startswith("https://"):
            raise ValueError("Teams webhook URL must be HTTPS")
        self._webhook_url = webhook_url.strip()
        self._timeout_seconds = timeout_seconds
        self._urlopen = urlopen

    @classmethod
    def from_environment(cls) -> "TeamsWebhookClient":
        """Build the client from one App Service environment setting."""

        webhook_url = os.environ.get("TEAMS_WEBHOOK_URL", "").strip()
        if not webhook_url:
            raise TeamsWebhookError("missing environment setting: TEAMS_WEBHOOK_URL")
        return cls(webhook_url)

    def post_run(self, run: RunRecord) -> None:
        """Post a completed quote run using the Workflow template's text input."""

        payload = json.dumps({"text": render_teams_message(run)}).encode("utf-8")
        request = urllib.request.Request(
            self._webhook_url,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            # Reading the small response ensures the request completes before
            # the background job is considered successful.
            with self._urlopen(request, timeout=self._timeout_seconds) as response:
                response.read(10_000)
        except urllib.error.HTTPError as exc:
            raise TeamsWebhookError(f"Teams workflow returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            # Never include the webhook URL in an exception because the URL is
            # itself a secret capable of posting into the channel.
            raise TeamsWebhookError(f"Teams workflow request failed: {type(exc).__name__}") from exc
