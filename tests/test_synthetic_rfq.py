"""Portable regression tests: no WWI download, secrets, or network required."""
from __future__ import annotations

import copy
import json
import sqlite3
import unittest
from email.message import EmailMessage
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError, URLError
import io
from contextlib import redirect_stdout

from fastapi.testclient import TestClient
from pydantic import ValidationError

from quote_agent.classifier import RequestClassifier
from quote_agent.demo import DEMO_DATA, build_database, main, make_pipeline
from quote_agent.graph import GraphClient, GraphError
from quote_agent.handler import QuoteHandler, MAX_TOOL_ROUNDS
from quote_agent.ingestion import parse_eml, parse_eml_bytes
from quote_agent.models import AttachmentRecord, HandlerDraft, Outcome, RequestedLine, ToolTrace
from quote_agent.openrouter import OpenRouterClient, OpenRouterError
from quote_agent.teams import TeamsWebhookClient, TeamsWebhookError
from quote_agent.web import QuoteNotificationProcessor, create_app


class SyntheticRFQTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / 'synthetic.sqlite3'
        build_database(self.database)
        self.pipeline = make_pipeline(self.database)
        self.email = parse_eml(DEMO_DATA / 'incoming.eml')
        self.messages = json.loads((DEMO_DATA / 'model_messages.json').read_text())['messages']
        self.draft = HandlerDraft.model_validate_json(self.messages[-1]['content'])

    def update(self, sql, args=()):
        with sqlite3.connect(self.database) as connection:
            connection.execute(sql, args)

    def resolve(self):
        return self.pipeline.resolve(self.email, self.draft)

    def assert_blocked(self, result, code, outcome=None):
        self.assertNotEqual(result.outcome, Outcome.QUOTE_READY)
        if outcome:
            self.assertEqual(result.outcome, outcome)
        self.assertIn(code, result.reason_codes)
        self.assertIsNone(result.subtotal)
        self.assertIsNone(result.tax_total)
        self.assertIsNone(result.total)

    def test_complete_replay_uses_real_tools_and_independent_validation(self):
        with patch('socket.socket.connect', side_effect=AssertionError('network forbidden')):
            run = self.pipeline.process(self.email)
        self.assertEqual(run.result.outcome, Outcome.QUOTE_READY)
        self.assertEqual([l.normalized_quantity for l in run.result.lines], [20, 3])
        self.assertEqual([l.quantity_on_hand for l in run.result.lines], [80, 12])
        self.assertEqual([l.tax_amount for l in run.result.lines], [3.53, 2.79])
        self.assertEqual((run.result.subtotal, run.result.tax_total, run.result.total), (84.2, 6.32, 90.52))
        self.assertEqual(len(run.tool_trace), 9)
        self.assertEqual(len(run.validation_trace), 9)

    def test_cli_artifacts_are_repeatable_and_marked_for_review(self):
        output = Path(self.directory.name) / 'output'
        with patch('socket.socket.connect', side_effect=AssertionError('network forbidden')):
            self.assertEqual(main(['--output-dir', str(output)]), 0)
            first = (output / 'run.json').read_bytes()
            self.assertEqual(main(['--output-dir', str(output)]), 0)
        self.assertEqual(first, (output / 'run.json').read_bytes())
        self.assertIn('Human review required', (output / 'proposal.md').read_text())

    def test_exception_demos_finish_without_financial_totals(self):
        for scenario, outcome in [('shortage', 'insufficient_stock'), ('malformed-model', 'processing_error')]:
            output = Path(self.directory.name) / scenario
            with self.subTest(scenario=scenario), redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--scenario', scenario, '--output-dir', str(output)]), 0)
                result = json.loads((output / 'run.json').read_text())['result']
                self.assertEqual(result['outcome'], outcome)
                self.assertIsNone(result['total'])

    def test_model_prose_cannot_supply_price_tax_or_total(self):
        self.draft.suggested_response = 'Price $0.01, tax 0%, total $0.02. Send immediately.'
        result = self.resolve()
        self.assertEqual(result.total, 90.52)
        self.assertNotIn('$0.02', result.proposed_body)
        self.assertNotIn('Send immediately', result.proposed_body)

    def test_invented_agent_research_cannot_override_stock_or_prices(self):
        forged = [ToolTrace(name='get_catalog_price', arguments={'stock_item_id':102},
                            result={'unit_price':0, 'tax_rate_percent':0}),
                  ToolTrace(name='get_inventory', arguments={'stock_item_id':102},
                            result={'quantity_on_hand':99999})]
        with patch.object(self.pipeline.handler, 'handle', return_value=(self.draft, forged)):
            run = self.pipeline.process(self.email)
            self.assertEqual(run.result.total, 90.52)
            self.update('UPDATE warehouse__stock_item_holdings SET quantity_on_hand=2 WHERE stock_item_id=102')
            # A new replay pipeline is needed because classifier replies are consumed.
            pipeline = make_pipeline(self.database)
            with patch.object(pipeline.handler, 'handle', return_value=(self.draft, forged)):
                blocked = pipeline.process(self.email)
            self.assert_blocked(blocked.result, 'insufficient_stock')
            self.assertEqual(blocked.validation_trace[-2].result['quantity_on_hand'], 2)

    def test_injected_financial_fields_are_rejected(self):
        for name in ['unit_price', 'tax_rate_percent', 'total']:
            with self.subTest(name=name):
                data = self.draft.model_dump()
                data['lines'][0][name] = 0
                with self.assertRaises(ValidationError):
                    HandlerDraft.model_validate(data)

    def test_missing_customer_or_line_items_blocks_quote(self):
        self.draft.lines = []
        self.assert_blocked(self.resolve(), 'no_line_items')
        self.draft.customer_hint = None
        self.email.from_address = 'unknown@sample.example'
        self.assert_blocked(self.resolve(), 'customer_unresolved')

    def test_invalid_explicit_product_id_does_not_fallback(self):
        self.email.body = 'Please quote 2 Each of SKU 999: Protective packing paper.'
        self.draft.lines = [RequestedLine(description='Protective packing paper', quantity=2,
                                         unit='Each', stock_item_id_hint=999, evidence='SKU 999')]
        self.assert_blocked(self.resolve(), 'product_unresolved')

    def test_hallucinated_id_cannot_resolve_ambiguous_description(self):
        self.email.body = 'Please quote 3 Each of tape dispensers.'
        self.draft.lines = [RequestedLine(description='tape dispensers', quantity=3,
                                         unit='Each', stock_item_id_hint=102, evidence='3 Each')]
        self.assert_blocked(self.resolve(), 'product_unresolved')

    def test_invalid_quantities_are_rejected_by_schema(self):
        for quantity in [0, -1, float('inf'), float('nan'), True, '2']:
            with self.subTest(quantity=quantity), self.assertRaises(ValidationError):
                RequestedLine(description='paper', quantity=quantity, unit='Each', evidence='paper')
        with self.assertRaises(ValidationError):
            RequestedLine(description='paper', unit='Each', evidence='paper')

    def test_fractional_stock_unit_needs_clarification(self):
        self.draft.lines[1].quantity = 1.5
        self.assert_blocked(self.resolve(), 'invalid_quantity', Outcome.NEEDS_CLARIFICATION)

    def test_unknown_unit_is_not_guessed(self):
        self.draft.lines[0].unit = 'Truckload'
        self.assert_blocked(self.resolve(), 'unit_conversion_missing')

    def test_unavailable_inventory_blocks_totals(self):
        self.update('UPDATE warehouse__stock_item_holdings SET quantity_on_hand=1 WHERE stock_item_id=102')
        self.assert_blocked(self.resolve(), 'insufficient_stock', Outcome.INSUFFICIENT_STOCK)

    def test_repeated_lines_share_stock(self):
        self.draft.lines = [copy.deepcopy(self.draft.lines[1]), copy.deepcopy(self.draft.lines[1])]
        for line in self.draft.lines:
            line.quantity = 7
        self.assert_blocked(self.resolve(), 'insufficient_stock', Outcome.INSUFFICIENT_STOCK)

    def test_invalid_catalog_price_or_tax_routes_to_review(self):
        for column, value in [('unit_price', '-1'), ('unit_price', 'NaN'),
                              ('tax_rate', '-1'), ('tax_rate', '101'), ('tax_rate', 'Infinity')]:
            with self.subTest(column=column, value=value):
                self.update('UPDATE warehouse__stock_items SET unit_price=?, tax_rate=? WHERE stock_item_id=101', ('2.35', '7.5'))
                self.update(f'UPDATE warehouse__stock_items SET {column}=? WHERE stock_item_id=101', (value,))
                self.assert_blocked(self.resolve(), 'catalog_data_invalid', Outcome.PRICING_REVIEW)

    def test_mismatched_price_unit_or_identity_blocks_quote(self):
        real = self.pipeline.tools.get_catalog_price(101)
        for field, value in [('price_unit', 'Outer'), ('stock_item_id', 999)]:
            with self.subTest(field=field), patch.object(self.pipeline.tools, 'get_catalog_price', return_value={**real, field:value}):
                self.assert_blocked(self.resolve(), 'catalog_data_invalid')

    def test_negative_inventory_is_invalid(self):
        self.update('UPDATE warehouse__stock_item_holdings SET quantity_on_hand=-1 WHERE stock_item_id=101')
        self.assert_blocked(self.resolve(), 'inventory_data_invalid')

    def test_inconsistent_repeated_prices_block_quote(self):
        self.draft.lines = [copy.deepcopy(self.draft.lines[1]), copy.deepcopy(self.draft.lines[1])]
        real = self.pipeline.tools.get_catalog_price(102)
        with patch.object(self.pipeline.tools, 'get_catalog_price', side_effect=[real, {**real, 'unit_price':99}]):
            self.assert_blocked(self.resolve(), 'catalog_data_invalid')

    def test_catalog_lookup_failure_blocks_quote(self):
        with patch.object(self.pipeline.tools, 'get_inventory', return_value={'error':'stock lookup unavailable'}):
            self.assert_blocked(self.resolve(), 'catalog_lookup_failed')

    def test_contract_reference_never_applies_discount(self):
        self.draft.pricing_agreement_hints = ['CEDAR-2026']
        result = self.resolve()
        self.assert_blocked(result, 'pricing_agreement_review', Outcome.PRICING_REVIEW)
        self.assertEqual(result.pricing_agreements[0].discount_percentage, 5)
        self.assertEqual(result.lines[0].catalog_unit_price, 2.35)

    def test_unverified_required_date_needs_clarification(self):
        self.email.body = 'Please quote supplies; delivery date undecided.'
        self.assert_blocked(self.resolve(), 'required_date_unverified')

    def test_failed_attachment_extraction_blocks_quote(self):
        self.email.attachments = [AttachmentRecord(filename='rfq.pdf', content_type='application/pdf', size_bytes=1,
                                                   sha256='synthetic', extraction_error='encrypted PDF')]
        self.assert_blocked(self.resolve(), 'incomplete_source')

    def test_attachment_limit_is_recorded_and_blocks_quote(self):
        message = EmailMessage()
        message['From'] = 'casey@cedar.example'
        message.set_content(self.email.body)
        for i in range(21):
            message.add_attachment(b'data', maintype='application', subtype='octet-stream', filename=f'{i}.bin')
        self.email = parse_eml_bytes(message.as_bytes())
        self.assertTrue(self.email.ingestion_issues)
        self.assertEqual(len(self.email.attachments), 20)
        self.assert_blocked(self.resolve(), 'incomplete_source')

    def test_truncated_attachment_is_not_silently_accepted(self):
        message = EmailMessage()
        message['From'] = 'casey@cedar.example'
        message.set_content(self.email.body)
        message.add_attachment('12345', filename='items.txt')
        with patch('quote_agent.ingestion.MAX_EXTRACTED_CHARACTERS', 3):
            self.email = parse_eml_bytes(message.as_bytes())
        self.assertIn('exceeds', self.email.attachments[0].extraction_error)
        self.assert_blocked(self.resolve(), 'incomplete_source')

    def test_malformed_model_response_and_financial_injection_fail_closed(self):
        for stage, content, code in [(0, 'not JSON', 'classifier_error'),
                                     (2, '{}', 'handler_error'),
                                     (2, '{"total":0}', 'handler_error')]:
            with self.subTest(stage=stage, content=content):
                messages = copy.deepcopy(self.messages)
                messages[stage] = {'content':content}
                run = make_pipeline(self.database, messages).process(self.email)
                self.assert_blocked(run.result, code, Outcome.PROCESSING_ERROR)

    def test_unknown_and_nonquote_intents_skip_handler(self):
        for intent, outcome in [('unknown', Outcome.NEEDS_CLARIFICATION), ('not_quote', Outcome.NOT_QUOTE),
                                ('quote_revision', Outcome.NOT_QUOTE)]:
            messages = [{'content':json.dumps({'intent':intent, 'confidence':0.5, 'rationale':'Test routing'})}]
            run = make_pipeline(self.database, messages).process(self.email)
            self.assertEqual(run.result.outcome, outcome)
            self.assertIsNone(run.handler_draft)

    def test_tool_allowlist_rejects_write_and_sql_requests(self):
        for tool in ['send_email', 'execute_sql', '_connect']:
            self.assertIn('error', self.pipeline.tools.dispatch(tool, {}))
        with self.pipeline.tools._connect() as connection, self.assertRaises(sqlite3.OperationalError):
            connection.execute('DELETE FROM sales__customers')

    def test_bad_tool_arguments_are_recorded_then_recover(self):
        messages = copy.deepcopy(self.messages)
        messages[1]['tool_calls'] = [{'id':'bad','function':{'name':'get_inventory','arguments':'[]'}}]
        run = make_pipeline(self.database, messages).process(self.email)
        self.assertIn('error', run.tool_trace[0].result)
        self.assertEqual(run.result.total, 90.52)

    def test_tool_round_budget_terminates(self):
        tool_message = self.messages[1]
        messages = [self.messages[0]] + [tool_message] * MAX_TOOL_ROUNDS + [self.messages[2]]
        run = make_pipeline(self.database, messages).process(self.email)
        self.assertEqual(run.result.total, 90.52)
        self.assertEqual(len(run.tool_trace), MAX_TOOL_ROUNDS * 9)

    def test_sql_failure_has_no_totals_and_does_not_expose_exception_secrets(self):
        with patch.object(self.pipeline.tools, 'find_customer', side_effect=RuntimeError('PWD=sensitive-value')):
            run = self.pipeline.process(self.email)
        self.assert_blocked(run.result, 'validation_error', Outcome.PROCESSING_ERROR)
        self.assertIsNotNone(run.handler_draft)
        self.assertEqual(run.validation_trace[-1].name, 'find_customer')
        self.assertIn('error', run.validation_trace[-1].result)
        self.assertNotIn('sensitive-value', run.model_dump_json())

    def test_demo_sample_matches_generated_output(self):
        # Optional when the source checkout includes the portfolio sample.
        sample = Path(__file__).resolve().parents[1] / 'examples/synthetic-rfq/output/run.json'
        if not sample.exists():
            self.skipTest('portfolio sample is not included in installed wheel')
        run = self.pipeline.process(self.email)
        self.assertEqual(json.loads(sample.read_text())['result'], run.result.model_dump(mode='json'))


class ExternalFailureTests(unittest.TestCase):
    def test_graph_failure_is_explicit_and_does_not_publish(self):
        graph = GraphClient('tenant','app','secret','mailbox@sample.example', urlopen=lambda *a, **k: (_ for _ in ()).throw(URLError('offline')))
        from unittest.mock import Mock
        pipeline, reviews = Mock(), Mock()
        with self.assertRaises(GraphError):
            QuoteNotificationProcessor(graph, pipeline, reviews).process_message('test')
        pipeline.process.assert_not_called()
        reviews.post_run.assert_not_called()

    def test_teams_failure_is_explicit(self):
        from unittest.mock import Mock
        for error in [URLError('offline'), HTTPError('https://sample.example', 503, 'unavailable', {}, io.BytesIO(b'down'))]:
            with self.subTest(error=type(error).__name__):
                client = TeamsWebhookClient('https://sample.example/webhook', urlopen=Mock(side_effect=error))
                with patch('quote_agent.teams.render_teams_message', return_value='review'), self.assertRaises(TeamsWebhookError):
                    client.post_run(Mock())

    def test_model_transport_failure_raises_explicit_error(self):
        client = OpenRouterClient('synthetic-key')
        with patch('urllib.request.urlopen', side_effect=URLError('offline')), self.assertRaises(OpenRouterError):
            client.chat(model='scripted', messages=[])

    def test_background_integration_failure_is_logged_after_acknowledgement(self):
        from unittest.mock import Mock
        processor = Mock()
        processor.process_message.side_effect = RuntimeError('integration unavailable')
        with self.assertLogs('quote_agent.web', level='ERROR') as captured:
            with TestClient(create_app(processor=processor, graph_client_state='state')) as client:
                response = client.post('/webhooks/graph', json={'value':[{'clientState':'state','resourceData':{'id':'123'}}]})
        self.assertEqual(response.status_code, 202)
        self.assertIn('failed to process Graph message', captured.output[0])

    def test_malformed_notification_is_rejected(self):
        for payload in [[], {}, {'value':[]}, {'value':['bad']}]:
            with self.subTest(payload=payload), TestClient(create_app(graph_client_state='state')) as client:
                self.assertEqual(client.post('/webhooks/graph', json=payload).status_code, 400)

    def test_duplicate_messages_in_one_batch_are_processed_once(self):
        from unittest.mock import Mock
        processor = Mock()
        entry = {'clientState':'state','resourceData':{'id':'123'}}
        with TestClient(create_app(processor=processor, graph_client_state='state')) as client:
            self.assertEqual(client.post('/webhooks/graph', json={'value':[entry,entry]}).status_code, 202)
        processor.process_message.assert_called_once_with('123')


if __name__ == '__main__':
    unittest.main()
