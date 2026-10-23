"""Small Microsoft Graph adapter for unattended shared-mailbox access.

The adapter deliberately uses ordinary HTTPS instead of hiding the protocol
behind a large SDK. That makes the Entra client-credential exchange and the
subsequent MIME download visible and straightforward to mock in tests.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from .ingestion import parse_eml_bytes
from .models import InboundEmail


GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
MAX_MIME_BYTES = 35 * 1024 * 1024


class GraphError(RuntimeError):
    """Raised when authentication or a Microsoft Graph request fails."""


class GraphClient:
    """Authenticate as the registered application and read mailbox messages."""

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        mailbox: str,
        *,
        timeout_seconds: float = 30.0,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
        clock: Callable[[], float] = time.time,
    ) -> None:
        required = {
            "tenant_id": tenant_id,
            "client_id": client_id,
            "client_secret": client_secret,
            "mailbox": mailbox,
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if missing:
            raise ValueError(f"missing Graph configuration: {', '.join(missing)}")

        self.tenant_id = tenant_id.strip()
        self.client_id = client_id.strip()
        self._client_secret = client_secret.strip()
        self.mailbox = mailbox.strip().lower()
        self._timeout_seconds = timeout_seconds
        self._urlopen = urlopen
        self._clock = clock
        self._access_token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = threading.Lock()

    @classmethod
    def from_environment(cls) -> "GraphClient":
        """Build a client from settings that work locally and in App Service."""

        names = (
            "MICROSOFT_TENANT_ID",
            "MICROSOFT_CLIENT_ID",
            "MICROSOFT_CLIENT_SECRET",
            "MICROSOFT_MAILBOX",
        )
        missing = [name for name in names if not os.environ.get(name, "").strip()]
        if missing:
            raise GraphError(f"missing environment settings: {', '.join(missing)}")
        return cls(
            tenant_id=os.environ["MICROSOFT_TENANT_ID"],
            client_id=os.environ["MICROSOFT_CLIENT_ID"],
            client_secret=os.environ["MICROSOFT_CLIENT_SECRET"],
            mailbox=os.environ["MICROSOFT_MAILBOX"],
        )

    def _token(self) -> str:
        """Return a cached Graph token, refreshing shortly before it expires."""

        # Multiple webhook notifications can arrive together. The lock avoids
        # making one token request per message when a batch is processed.
        with self._token_lock:
            if self._access_token and self._clock() < self._token_expires_at - 60:
                return self._access_token

            token_url = (
                "https://login.microsoftonline.com/"
                f"{urllib.parse.quote(self.tenant_id, safe='')}/oauth2/v2.0/token"
            )
            body = urllib.parse.urlencode(
                {
                    "client_id": self.client_id,
                    "client_secret": self._client_secret,
                    "scope": "https://graph.microsoft.com/.default",
                    "grant_type": "client_credentials",
                }
            ).encode("ascii")
            request = urllib.request.Request(
                token_url,
                data=body,
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            raw = self._open(request, maximum_bytes=1_000_000)
            try:
                response = json.loads(raw.decode("utf-8"))
                token = response["access_token"]
                expires_in = int(response.get("expires_in", 3600))
            except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise GraphError("Entra returned an unexpected token response") from exc
            if not isinstance(token, str) or not token:
                raise GraphError("Entra returned an empty access token")

            self._access_token = token
            self._token_expires_at = self._clock() + max(60, expires_in)
            return token

    def _open(self, request: urllib.request.Request, *, maximum_bytes: int) -> bytes:
        """Execute one bounded request without ever including credentials in errors."""

        try:
            with self._urlopen(request, timeout=self._timeout_seconds) as response:
                payload = response.read(maximum_bytes + 1)
        except urllib.error.HTTPError as exc:
            # Graph error bodies contain useful request IDs but never need our
            # Authorization header or client secret to diagnose the failure.
            detail = exc.read(1_000).decode("utf-8", errors="replace")
            raise GraphError(f"Microsoft request failed with HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise GraphError(f"Microsoft request failed: {exc}") from exc
        if len(payload) > maximum_bytes:
            raise GraphError(f"Microsoft response exceeded {maximum_bytes} bytes")
        return payload

    def get_message_mime(self, message_id: str) -> bytes:
        """Download the original RFC 822 message, including its attachments."""

        if not message_id.strip():
            raise ValueError("message_id is required")
        mailbox = urllib.parse.quote(self.mailbox, safe="")
        encoded_id = urllib.parse.quote(message_id.strip(), safe="")
        request = urllib.request.Request(
            f"{GRAPH_ROOT}/users/{mailbox}/messages/{encoded_id}/$value",
            method="GET",
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Accept": "message/rfc822",
            },
        )
        return self._open(request, maximum_bytes=MAX_MIME_BYTES)

    def get_inbound_email(self, message_id: str) -> InboundEmail:
        """Download and parse a Graph message through the fixture-tested parser."""

        mime = self.get_message_mime(message_id)
        source_name = f"graph://{self.mailbox}/messages/{message_id}"
        return parse_eml_bytes(mime, source_name=source_name)

    def _graph_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call a small Graph JSON endpoint using the same cached app token."""

        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"{GRAPH_ROOT}{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if body is not None else {}),
            },
        )
        raw = self._open(request, maximum_bytes=1_000_000)
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GraphError("Microsoft Graph returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise GraphError("Microsoft Graph returned an unexpected JSON value")
        return result

    def create_or_renew_inbox_subscription(
        self,
        notification_url: str,
        client_state: str,
        *,
        lifetime_days: int = 6,
        now: datetime | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Create or renew the one Outlook Inbox webhook used by this app.

        Outlook webhook subscriptions expire in under seven days. For this
        first version, this method makes manual renewal one command and avoids
        creating duplicates when the URL and client state have not changed.
        """

        if not notification_url.strip().lower().startswith("https://"):
            raise ValueError("notification_url must be a public HTTPS URL")
        if not client_state.strip():
            raise ValueError("client_state is required")
        if not 1 <= lifetime_days <= 6:
            raise ValueError("lifetime_days must be between 1 and 6")

        resource = f"users/{self.mailbox}/mailFolders('Inbox')/messages"
        current_time = now or datetime.now(timezone.utc)
        expiration = current_time.astimezone(timezone.utc) + timedelta(days=lifetime_days)
        expiration_text = expiration.isoformat(timespec="seconds").replace("+00:00", "Z")

        # Listing first makes this command safe to rerun for weekly renewal.
        existing = self._graph_json("GET", "/subscriptions").get("value", [])
        if not isinstance(existing, list):
            raise GraphError("Microsoft Graph returned an invalid subscription list")
        for item in existing:
            if not isinstance(item, dict):
                continue
            if (
                item.get("resource") == resource
                and item.get("notificationUrl") == notification_url.strip()
                and item.get("clientState") == client_state.strip()
                and isinstance(item.get("id"), str)
            ):
                subscription_id = urllib.parse.quote(item["id"], safe="")
                renewed = self._graph_json(
                    "PATCH",
                    f"/subscriptions/{subscription_id}",
                    {"expirationDateTime": expiration_text},
                )
                return "renewed", renewed

        created = self._graph_json(
            "POST",
            "/subscriptions",
            {
                "changeType": "created",
                "notificationUrl": notification_url.strip(),
                "resource": resource,
                "expirationDateTime": expiration_text,
                "clientState": client_state.strip(),
                "latestSupportedTlsVersion": "v1_2",
            },
        )
        return "created", created
