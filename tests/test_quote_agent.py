from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from quote_agent.classifier import RequestClassifier
from quote_agent.handler import QuoteHandler
from quote_agent.ingestion import parse_eml
from quote_agent.models import Classification, HandlerDraft, Intent, Outcome, RequestedLine
from quote_agent.openrouter import OpenRouterReply, load_api_key
from quote_agent.pipeline import QuotePipeline
from quote_agent.render import render_markdown
from quote_agent.tools import QuoteTools


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "data" / "portfolio_artifacts" / "02_inbound_quote"
EMAILS = FIXTURES / "emails"
DATABASE = ROOT / "data" / "sqlite" / "wide_world_importers.sqlite3"


class _UnusedAgent:
    """Resolver tests do not make model calls, but the pipeline needs collaborators."""

    model = "test/no-model"


class _ReplySequence:
    """Small fake client that exercises agent protocol code without network calls."""

    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return next(self.replies)


class _StaticClassifier:
    model = "test/classifier"

    def classify(self, email):
        return Classification(
            intent=Intent.NEW_QUOTE_REQUEST,
            confidence=0.99,
            rationale="The sender asks for a product quote.",
            evidence=["Please quote"],
        )


class _StaticHandler:
    model = "test/handler"

    def handle(self, email):
        return (
            HandlerDraft(
                customer_hint="Tailspin Toys",
                required_date="2016-06-24",
                lines=[
                    RequestedLine(
                        description="USB missile launcher (Green)",
                        quantity=24,
                        unit="Each",
                        stock_item_id_hint=1,
                        evidence="24 USB missile launcher (Green), stock item 1",
                    )
                ],
                summary="One exact item",
                suggested_response="Draft only",
            ),
            [],
        )


@unittest.skipUnless(EMAILS.exists(), "optional legacy RFQ fixtures are not generated")
class EmailIngestionTests(unittest.TestCase):
    def test_all_eight_sample_emails_parse(self):
        paths = sorted(EMAILS.glob("*.eml"))
        self.assertEqual(len(paths), 8)
        parsed = [parse_eml(path) for path in paths]
        self.assertTrue(all(email.subject and email.body and email.from_address for email in parsed))

    def test_spreadsheet_attachments_are_extracted_as_rows(self):
        restock = parse_eml(EMAILS / "rfq_003_spreadsheet.eml")
        mixed_units = parse_eml(EMAILS / "rfq_006_mixed_units.eml")
        self.assertIn("USB food flash drive - banana\t120\tEach", restock.attachments[0].extracted_text)
        self.assertIn("brown shipping carton 305 mm\t20\tOuter\tSKU 184", mixed_units.attachments[0].extracted_text)
        self.assertIsNone(restock.attachments[0].extraction_error)


class CredentialTests(unittest.TestCase):
    def test_single_trailing_copy_artifact_is_removed(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "key.txt"
            path.write_text("sk-or-v1-" + "a" * 64 + "%", encoding="utf-8")
            path.chmod(0o600)
            with self.assertWarnsRegex(UserWarning, "trailing non-hex"):
                key = load_api_key(path)
            self.assertEqual(key, "sk-or-v1-" + "a" * 64)


@unittest.skipUnless(DATABASE.exists() and EMAILS.exists(), "optional WWI database and RFQ fixtures are not built")
class AgentProtocolTests(unittest.TestCase):
    def test_classifier_validates_structured_json(self):
        client = _ReplySequence(
            [
                OpenRouterReply(
                    message={
                        "content": (
                            '{"intent":"new_quote_request","confidence":0.99,'
                            '"rationale":"Requests a quote","evidence":["Please quote"]}'
                        )
                    },
                    model="test/classifier",
                    usage={},
                )
            ]
        )
        result = RequestClassifier(client).classify(parse_eml(EMAILS / "rfq_001_exact.eml"))
        self.assertEqual(result.intent, Intent.NEW_QUOTE_REQUEST)
        self.assertEqual(client.calls[0]["response_model"].__name__, "Classification")

    def test_handler_executes_only_registered_tool_then_returns_draft(self):
        client = _ReplySequence(
            [
                OpenRouterReply(
                    message={
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "get_inventory",
                                    "arguments": '{"stock_item_id":1}',
                                },
                            }
                        ],
                    },
                    model="test/handler",
                    usage={},
                ),
                OpenRouterReply(
                    message={
                        "content": (
                            '{"customer_hint":"Tailspin Toys","required_date":"2016-06-24",'
                            '"pricing_agreement_hints":[],"partial_shipment_allowed":null,'
                            '"lines":[{"description":"USB missile launcher (Green)",'
                            '"quantity":24,"unit":"Each","stock_item_id_hint":1,'
                            '"evidence":"stock item 1"}],"summary":"One item",'
                            '"suggested_response":"Draft only"}'
                        )
                    },
                    model="test/handler",
                    usage={},
                ),
            ]
        )
        tools = QuoteTools(DATABASE, FIXTURES / "reference")
        draft, trace = QuoteHandler(client, tools).handle(
            parse_eml(EMAILS / "rfq_001_exact.eml")
        )
        self.assertEqual(draft.lines[0].stock_item_id_hint, 1)
        self.assertEqual(trace[0].name, "get_inventory")
        self.assertEqual(trace[0].result["quantity_on_hand"], 175609)
        self.assertEqual(client.calls[1]["messages"][-1]["role"], "tool")

    def test_offline_end_to_end_run_renders_review_markdown(self):
        tools = QuoteTools(DATABASE, FIXTURES / "reference")
        run = QuotePipeline(_StaticClassifier(), _StaticHandler(), tools).run(
            EMAILS / "rfq_001_exact.eml"
        )
        report = render_markdown(run)
        self.assertEqual(run.result.outcome, Outcome.QUOTE_READY)
        self.assertIn("**Total:** $690.00", report)
        self.assertIn("## Original email", report)


@unittest.skipUnless(DATABASE.exists() and EMAILS.exists(), "optional WWI database and RFQ fixtures are not built")
class QuoteToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = QuoteTools(DATABASE, FIXTURES / "reference")

    def test_customer_is_resolved_from_sender(self):
        match = self.tools.find_customer("waldemar@tailspintoys.com", "Tailspin Toys")
        self.assertEqual(match["customer_id"], 1)
        self.assertEqual(match["match_type"], "sender_email")

    def test_catalog_search_preserves_ambiguity(self):
        candidates = self.tools.search_products("USB launchers")["candidates"]
        self.assertEqual({item["stock_item_id"] for item in candidates[:2]}, {1, 2})
        self.assertTrue(all(item["match_type"] == "search" for item in candidates[:2]))

    def test_known_typo_uses_curated_alias(self):
        candidate = self.tools.search_products("bananna USB food flash drives")["candidates"][0]
        self.assertEqual(candidate["stock_item_id"], 9)
        self.assertEqual(candidate["match_type"], "alias")

    def test_outer_is_converted_to_stock_units(self):
        conversion = self.tools.normalize_quantity(184, 20, "Outer")
        self.assertEqual(conversion["normalized_quantity"], 500)
        self.assertEqual(conversion["normalized_unit"], "Each")

    def test_q2_pricing_agreement_is_resolved_for_email_date(self):
        result = self.tools.find_pricing_agreements(
            1,
            "2016-06-09",
            "current Q2 Tailspin agreement",
        )
        self.assertEqual([item["agreement_id"] for item in result["candidates"]], ["TT-Q2-2016"])


@unittest.skipUnless(DATABASE.exists() and EMAILS.exists(), "optional WWI database and RFQ fixtures are not built")
class DeterministicOutcomeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = QuoteTools(DATABASE, FIXTURES / "reference")
        cls.pipeline = QuotePipeline(_UnusedAgent(), _UnusedAgent(), cls.tools)

    def resolve(self, filename: str, draft: HandlerDraft):
        return self.pipeline.resolve(parse_eml(EMAILS / filename), draft)

    def test_exact_item_produces_quote_with_python_calculated_totals(self):
        result = self.resolve(
            "rfq_001_exact.eml",
            HandlerDraft(
                customer_hint="Tailspin Toys",
                # Deliberately wrong: source evidence must win over model output.
                required_date="2024-06-24",
                lines=[
                    RequestedLine(
                        description="USB missile launcher (Green)",
                        quantity=24,
                        unit="Each",
                        stock_item_id_hint=1,
                        evidence="24 each of Stock Item 1",
                    )
                ],
                summary="Exact catalog RFQ",
                suggested_response="Model prose is not used for financial decisions.",
            ),
        )
        self.assertEqual(result.outcome, Outcome.QUOTE_READY)
        self.assertEqual(result.lines[0].stock_item_id, 1)
        self.assertEqual(result.subtotal, 600)
        self.assertEqual(result.tax_total, 90)
        self.assertEqual(result.total, 690)
        self.assertIn("2016-06-24", result.proposed_body)
        self.assertNotIn("2024-06-24", result.proposed_body)

    def test_ambiguous_product_requires_clarification(self):
        result = self.resolve(
            "rfq_002_ambiguous.eml",
            HandlerDraft(
                customer_hint="Tailspin Toys",
                lines=[
                    RequestedLine(
                        description="USB launchers",
                        quantity=50,
                        unit="Each",
                        evidence="50 USB launchers",
                    )
                ],
                summary="Color is unspecified",
                suggested_response="Ask which launcher.",
            ),
        )
        self.assertEqual(result.outcome, Outcome.NEEDS_CLARIFICATION)
        self.assertIn("product_unresolved", result.reason_codes)

    def test_spreadsheet_lines_produce_one_quote(self):
        result = self.resolve(
            "rfq_003_spreadsheet.eml",
            HandlerDraft(
                customer_hint="Tailspin Toys",
                required_date="2016-06-24",
                lines=[
                    RequestedLine(description="USB food flash drive - banana", quantity=120, unit="Each", evidence="spreadsheet row"),
                    RequestedLine(description="USB food flash drive - chocolate bar", quantity=80, unit="Each", evidence="spreadsheet row"),
                    RequestedLine(description="USB food flash drive - dessert 10 drive variety pack", quantity=12, unit="Packet", evidence="spreadsheet row"),
                ],
                summary="Store restock",
                suggested_response="Draft only",
            ),
        )
        self.assertEqual(result.outcome, Outcome.QUOTE_READY)
        self.assertEqual([line.stock_item_id for line in result.lines], [9, 10, 15])
        self.assertEqual(result.total, 10672)

    def test_alias_can_resolve_misspelling(self):
        result = self.resolve(
            "rfq_004_misspelling.eml",
            HandlerDraft(
                customer_hint="Tailspin Toys",
                lines=[
                    RequestedLine(
                        description="bananna USB food flash drives",
                        quantity=100,
                        unit="Each",
                        evidence="100 bananna USB food flash drives",
                    )
                ],
                summary="Misspelled item",
                suggested_response="Draft only",
            ),
        )
        self.assertEqual(result.outcome, Outcome.QUOTE_READY)
        self.assertEqual(result.lines[0].stock_item_id, 9)
        self.assertEqual(result.lines[0].match_type, "alias")

    def test_missing_carton_dimension_is_not_guessed(self):
        result = self.resolve(
            "rfq_005_missing_spec.eml",
            HandlerDraft(
                customer_hint="Tailspin Toys",
                lines=[
                    RequestedLine(
                        description="brown shipping cartons",
                        quantity=20,
                        unit="Outer",
                        evidence="20 outers of brown shipping cartons",
                    )
                ],
                summary="Carton size omitted",
                suggested_response="Ask for dimensions.",
            ),
        )
        self.assertEqual(result.outcome, Outcome.NEEDS_CLARIFICATION)
        self.assertIn("product_unresolved", result.reason_codes)

    def test_uom_conversion_and_shortage_are_both_preserved(self):
        result = self.resolve(
            "rfq_006_mixed_units.eml",
            HandlerDraft(
                customer_hint="Tailspin Toys",
                lines=[
                    RequestedLine(description="brown shipping carton 305 mm", quantity=20, unit="Outer", stock_item_id_hint=184, evidence="SKU 184"),
                    RequestedLine(description="fragile tape 48x75 black orange", quantity=6, unit="Outer", stock_item_id_hint=191, evidence="SKU 191"),
                    RequestedLine(description="red tape dispenser", quantity=15, unit="Each", stock_item_id_hint=204, evidence="SKU 204"),
                ],
                summary="Mixed unit request",
                suggested_response="Draft only",
            ),
        )
        self.assertEqual(result.outcome, Outcome.INSUFFICIENT_STOCK)
        self.assertIn("uom_conversion", result.reason_codes)
        self.assertIn("insufficient_stock", result.reason_codes)
        self.assertEqual([line.stock_item_id for line in result.lines], [184, 191, 204])
        self.assertEqual(result.lines[0].normalized_quantity, 500)

    def test_large_order_terminates_as_insufficient_stock(self):
        result = self.resolve(
            "rfq_007_insufficient_stock.eml",
            HandlerDraft(
                customer_hint="Tailspin Toys",
                partial_shipment_allowed=False,
                lines=[
                    RequestedLine(
                        description="Shipping carton (Brown) 305x305x305mm",
                        quantity=10_000,
                        unit="Outer",
                        stock_item_id_hint=184,
                        evidence="10,000 outers of stock item 184",
                    )
                ],
                summary="Quantity exceeds stock",
                suggested_response="Cannot quote full quantity.",
            ),
        )
        self.assertEqual(result.outcome, Outcome.INSUFFICIENT_STOCK)
        self.assertIsNone(result.total)

    def test_contract_reference_routes_to_pricing_review(self):
        result = self.resolve(
            "rfq_008_customer_price.eml",
            HandlerDraft(
                customer_hint="Tailspin Toys",
                pricing_agreement_hints=["Q2 Tailspin agreement"],
                lines=[
                    RequestedLine(
                        description="USB food variety packs",
                        quantity=40,
                        unit="Each",
                        evidence="40 USB food variety packs",
                    )
                ],
                summary="Special price requested",
                suggested_response="Route for pricing review.",
            ),
        )
        self.assertEqual(result.outcome, Outcome.PRICING_REVIEW)
        self.assertIn("pricing_agreement_review", result.reason_codes)
        self.assertEqual(result.pricing_agreements[0].agreement_id, "TT-Q2-2016")
        self.assertIsNone(result.total)


if __name__ == "__main__":
    unittest.main()
