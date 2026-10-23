"""One-command setup and manual renewal for the Outlook Inbox webhook."""

from __future__ import annotations

import argparse
import os
import sys

from .graph import GraphClient


def build_parser() -> argparse.ArgumentParser:
    """Keep the only changing deployment value explicit at the command line."""

    parser = argparse.ArgumentParser(
        description="Create or renew the quote mailbox's Microsoft Graph webhook."
    )
    parser.add_argument(
        "--notification-url",
        default=os.environ.get("GRAPH_NOTIFICATION_URL"),
        help=(
            "Public endpoint ending in /webhooks/graph; alternatively set "
            "GRAPH_NOTIFICATION_URL"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Create or renew a six-day subscription without printing any secrets."""

    args = build_parser().parse_args(argv)
    if not args.notification_url:
        print("missing --notification-url", file=sys.stderr)
        return 2

    try:
        client_state = os.environ.get("GRAPH_CLIENT_STATE", "")
        action, subscription = GraphClient.from_environment().create_or_renew_inbox_subscription(
            args.notification_url,
            client_state,
        )
        print(f"Subscription {action}.")
        print(f"ID: {subscription.get('id', '(not returned)')}")
        print(f"Resource: {subscription.get('resource', '(not returned)')}")
        print(f"Expires: {subscription.get('expirationDateTime', '(not returned)')}")
        return 0
    except Exception as exc:
        # The Graph adapter already strips credentials from its errors.
        print(f"subscription failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
