from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import parse_qs

from fastapi.testclient import TestClient

from quote_agent.azure_sql import AzureSqlQuoteTools
from quote_agent.graph import GraphClient
from quote_agent.ingestion import parse_eml_bytes
from quote_agent.models import RunRecord
from quote_agent.teams import TeamsWebhookClient, render_teams_message
from quote_agent.web import create_app


SIMPLE_MIME = (
    b"From: Ada Buyer <ada@example.com>\r\n"
    b"To: quotes@obelyr.com\r\n"
    b"Message-ID: <rfq-1@example.com>\r\n"
    b"Subject: Please quote this\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Please quote 10 widgets.\r\n"
)


class _FakeHttpResponse:
    """Minimal urllib response context used to test real request construction."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, maximum: int = -1) -> bytes:
        return self.payload if maximum < 0 else self.payload[:maximum]


class _FakeUrlOpen:
    def __init__(self) -> None:
        self.requests = []

    def __call__(self, request, *, timeout):
        self.requests.append((request, timeout))
        if "login.microsoftonline.com" in request.full_url:
            return _FakeHttpResponse(
                json.dumps({"access_token": "test-token", "expires_in": 3600}).encode()
            )
        return _FakeHttpResponse(SIMPLE_MIME)


class InMemoryIngestionTests(unittest.TestCase):
    def test_parse_eml_bytes_supports_graph_mime(self):
        email = parse_eml_bytes(SIMPLE_MIME, source_name="graph://test/message-1")

        self.assertEqual(email.source_path, "graph://test/message-1")
        self.assertEqual(email.from_address, "ada@example.com")
        self.assertEqual(email.subject, "Please quote this")
        self.assertIn("10 widgets", email.body)


class GraphClientTests(unittest.TestCase):
    def test_client_authenticates_and_downloads_encoded_message_id(self):
        opener = _FakeUrlOpen()
        client = GraphClient(
            "tenant-id",
            "client-id",
            "client-secret+with/specials",
            "quotes@obelyr.com",
            urlopen=opener,
            clock=lambda: 100.0,
        )

        email = client.get_inbound_email("message+/=id")

        self.assertEqual(email.message_id, "<rfq-1@example.com>")
        self.assertEqual(len(opener.requests), 2)
        token_request = opener.requests[0][0]
        token_form = parse_qs(token_request.data.decode("ascii"))
        self.assertEqual(token_form["client_id"], ["client-id"])
        self.assertEqual(token_form["client_secret"], ["client-secret+with/specials"])
        message_request = opener.requests[1][0]
        self.assertIn("quotes%40obelyr.com", message_request.full_url)
        self.assertIn("message%2B%2F%3Did", message_request.full_url)
        self.assertEqual(message_request.headers["Authorization"], "Bearer test-token")

    def test_inbox_subscription_is_created_with_a_six_day_expiration(self):
        responses = iter(
            [
                {"access_token": "test-token", "expires_in": 3600},
                {"value": []},
                {
                    "id": "subscription-1",
                    "resource": "users/quotes@obelyr.com/mailFolders('Inbox')/messages",
                    "expirationDateTime": "2026-09-20T12:00:00Z",
                },
            ]
        )
        requests = []

        def open_json(request, *, timeout):
            requests.append((request, timeout))
            return _FakeHttpResponse(json.dumps(next(responses)).encode())

        client = GraphClient(
            "tenant-id",
            "client-id",
            "client-secret",
            "quotes@obelyr.com",
            urlopen=open_json,
        )

        action, subscription = client.create_or_renew_inbox_subscription(
            "https://quote-agent.example/webhooks/graph",
            "random-client-state",
            now=datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(action, "created")
        self.assertEqual(subscription["id"], "subscription-1")
        self.assertEqual(requests[1][0].method, "GET")
        create_request = requests[2][0]
        self.assertEqual(create_request.method, "POST")
        payload = json.loads(create_request.data.decode("utf-8"))
        self.assertEqual(
            payload["resource"],
            "users/quotes@obelyr.com/mailFolders('Inbox')/messages",
        )
        self.assertEqual(payload["expirationDateTime"], "2026-09-20T12:00:00Z")
        self.assertEqual(payload["clientState"], "random-client-state")


def _sample_run() -> RunRecord:
    """Return the smallest realistic completed run for delivery tests."""

    return RunRecord.model_validate(
        {
            "run_id": "run-123",
            "created_at": "2026-09-14T12:00:00+00:00",
            "classifier_model": "test-classifier",
            "email": {
                "source_path": "graph://quotes/message-1",
                "sender": "Ada Buyer",
                "from_address": "ada@example.com",
                "subject": "Please quote shirts",
                "body": "Please quote ten black shirts.",
            },
            "classification": {
                "intent": "new_quote_request",
                "confidence": 0.99,
                "rationale": "The sender requested a quote.",
            },
            "result": {
                "outcome": "quote_ready",
                "subtotal": 180.0,
                "tax_total": 12.6,
                "total": 192.6,
                "proposed_subject": "Re: Please quote shirts",
                "proposed_body": "We can supply the requested shirts.",
                "lines": [
                    {
                        "requested_description": "black shirts",
                        "requested_quantity": 10,
                        "requested_unit": "Each",
                        "stock_item_id": 97,
                        "stock_item_name": "Black shirt 3XL",
                        "quantity_on_hand": 525771,
                        "line_total": 192.6,
                    }
                ],
            },
        }
    )


class TeamsWebhookTests(unittest.TestCase):
    def test_quote_run_is_rendered_as_a_readable_review(self):
        message = render_teams_message(_sample_run())

        self.assertIn("Quote review: Quote Ready", message)
        self.assertIn("Ada Buyer", message)
        self.assertIn("Black shirt 3XL", message)
        self.assertIn("$192.60", message)
        self.assertIn("Suggested email response", message)

    def test_workflow_receives_the_documented_text_payload(self):
        opener = _FakeUrlOpen()
        client = TeamsWebhookClient(
            "https://example.invalid/secret-workflow-url",
            urlopen=opener,
        )

        client.post_run(_sample_run())

        request = opener.requests[0][0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.headers["Content-type"], "application/json")
        self.assertIn("Quote review: Quote Ready", payload["text"])


class _RecordingProcessor:
    def __init__(self) -> None:
        self.message_ids: list[str] = []

    def process_message(self, message_id: str):
        self.message_ids.append(message_id)
        return None


class WebAppTests(unittest.TestCase):
    def test_health_does_not_initialize_cloud_dependencies(self):
        with TestClient(create_app()) as client:
            response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_graph_validation_token_is_returned_as_plain_text(self):
        with TestClient(create_app()) as client:
            response = client.post(
                "/webhooks/graph",
                params={"validationToken": "graph validation + token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "graph validation + token")
        self.assertTrue(response.headers["content-type"].startswith("text/plain"))

    def test_valid_notification_is_acknowledged_and_processed(self):
        processor = _RecordingProcessor()
        application = create_app(processor=processor, graph_client_state="shared-state")
        payload = {
            "value": [
                {
                    "changeType": "created",
                    "clientState": "shared-state",
                    "resourceData": {"id": "message-123"},
                }
            ]
        }

        with TestClient(application) as client:
            response = client.post("/webhooks/graph", json=payload)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(processor.message_ids, ["message-123"])

    def test_notification_with_wrong_client_state_is_rejected(self):
        application = create_app(
            processor=_RecordingProcessor(),
            graph_client_state="expected",
        )
        payload = {
            "value": [
                {
                    "clientState": "attacker-value",
                    "resourceData": {"id": "message-123"},
                }
            ]
        }

        with TestClient(application) as client:
            response = client.post("/webhooks/graph", json=payload)

        self.assertEqual(response.status_code, 401)


class _FakeCursor:
    def __init__(self, calls: list[tuple[str, tuple]]) -> None:
        self.calls = calls
        self.description = []
        self._rows = []

    def execute(self, sql: str, parameters: tuple = ()) -> None:
        self.calls.append((sql, parameters))
        if "[Fact].[Stock Holding]" in sql:
            names = (
                "stock_item_id",
                "stock_item_name",
                "quantity_per_outer",
                "unit_price",
                "tax_rate",
                "unit_name",
                "quantity_on_hand",
            )
            self._rows = [
                (97, '"The Gu" red shirt XML tag t-shirt (Black) 3XL', 1, Decimal("18.00"), Decimal("7.000"), "Each", 525771)
            ]
        elif "[Dimension].[Customer]" in sql:
            names = ("customer_id", "customer_name", "primary_contact")
            self._rows = [(1, "Tailspin Toys", "Waldemar Lantz")]
        else:  # pragma: no cover - catches accidental unregistered SQL in development.
            raise AssertionError(f"unexpected SQL: {sql}")
        self.description = [(name,) for name in names]

    def fetchall(self):
        return self._rows

    def close(self) -> None:
        pass


class _FakeConnection:
    def __init__(self, calls: list[tuple[str, tuple]]) -> None:
        self.calls = calls

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.calls)

    def close(self) -> None:
        pass


class AzureSqlToolTests(unittest.TestCase):
    def test_dw_catalog_is_mapped_to_the_existing_tool_contract(self):
        calls: list[tuple[str, tuple]] = []
        tools = AzureSqlQuoteTools(
            "Server=fake;Database=fake;",
            connect=lambda _: _FakeConnection(calls),
        )

        inventory = tools.get_inventory(97)
        price = tools.get_catalog_price(97)

        self.assertEqual(inventory["quantity_on_hand"], 525771)
        self.assertEqual(inventory["stock_unit"], "Each")
        self.assertEqual(price["unit_price"], 18.0)
        self.assertEqual(price["tax_rate_percent"], 7.0)
        # Inventory and price share a short catalog cache during one quote.
        self.assertEqual(len(calls), 1)

    def test_customer_hint_uses_parameterized_dw_query(self):
        calls: list[tuple[str, tuple]] = []
        tools = AzureSqlQuoteTools(
            "Server=fake;Database=fake;",
            connect=lambda _: _FakeConnection(calls),
        )

        result = tools.find_customer("waldemar@example.com", "Tailspin Toys")

        self.assertEqual(result["customer_id"], 1)
        self.assertEqual(result["match_type"], "name_hint")
        self.assertEqual(calls[0][1], ("%tailspin toys%", "tailspin toys"))


if __name__ == "__main__":
    unittest.main()
