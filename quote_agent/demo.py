"""Network-free RFQ replay using real ingestion, agents, tools and validation.

Only model responses are scripted. No credential loader or cloud client is
constructed. The tiny synthetic database is rebuilt for every demonstration.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

from .classifier import RequestClassifier
from .handler import QuoteHandler
from .models import Outcome
from .openrouter import OpenRouterReply
from .pipeline import QuotePipeline
from .render import render_markdown
from .tools import QuoteTools

DEMO_DATA = Path(__file__).with_name("demo_data")


class ReplayClient:
    """Inject authored replies at the model transport boundary."""

    def __init__(self, messages: list[dict]) -> None:
        self.messages = iter(messages)

    def chat(self, **kwargs) -> OpenRouterReply:
        return OpenRouterReply(message=next(self.messages), model=kwargs["model"], usage={})


def build_database(path: Path) -> None:
    """Build only the WWI-shaped tables used by the existing read-only tools."""
    fixture = json.loads((DEMO_DATA / "catalog.json").read_text())
    with sqlite3.connect(path) as connection:
        connection.executescript('''
            CREATE TABLE application__people (
                person_id INTEGER PRIMARY KEY, full_name TEXT, email_address TEXT, valid_to TEXT);
            CREATE TABLE sales__customers (
                customer_id INTEGER PRIMARY KEY, customer_name TEXT,
                primary_contact_person_id INTEGER, alternate_contact_person_id INTEGER, valid_to TEXT);
            CREATE TABLE warehouse__package_types (
                package_type_id INTEGER PRIMARY KEY, package_type_name TEXT);
            CREATE TABLE warehouse__stock_items (
                stock_item_id INTEGER PRIMARY KEY, stock_item_name TEXT, quantity_per_outer INTEGER,
                unit_price TEXT, tax_rate TEXT, unit_package_id INTEGER, valid_to TEXT);
            CREATE TABLE warehouse__stock_item_holdings (
                stock_item_id INTEGER PRIMARY KEY, quantity_on_hand INTEGER);
        ''')
        c = fixture['customer']
        connection.execute('INSERT INTO application__people VALUES (1,?,?,?)',
                           (c['contact'], c['email'], '9999-12-31'))
        connection.execute('INSERT INTO sales__customers VALUES (?,?,1,NULL,?)',
                           (c['id'], c['name'], '9999-12-31'))
        connection.execute("INSERT INTO warehouse__package_types VALUES (1,'Each')")
        for p in fixture['products']:
            connection.execute('INSERT INTO warehouse__stock_items VALUES (?,?,?,?,?,1,?)',
                               (p['id'], p['name'], p['outer'], p['price'], p['tax'], '9999-12-31'))
            connection.execute('INSERT INTO warehouse__stock_item_holdings VALUES (?,?)',
                               (p['id'], p['stock']))


def make_pipeline(database: Path, messages: list[dict] | None = None) -> QuotePipeline:
    if messages is None:
        messages = json.loads((DEMO_DATA / 'model_messages.json').read_text())['messages']
    tools = QuoteTools(database, DEMO_DATA / 'reference')
    tools.price_source = 'Synthetic demonstration catalog'
    client = ReplayClient(messages)
    return QuotePipeline(
        RequestClassifier(client, model='offline/scripted-classifier'),
        QuoteHandler(client, tools, model='offline/scripted-handler'), tools,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--scenario', choices=['standard', 'shortage', 'malformed-model'], default='standard')
    args = parser.parse_args(argv)
    with TemporaryDirectory(prefix='quote-demo-') as directory:
        database = Path(directory) / 'synthetic.sqlite3'
        build_database(database)
        messages = json.loads((DEMO_DATA / 'model_messages.json').read_text())['messages']
        if args.scenario == 'shortage':
            with sqlite3.connect(database) as connection:
                connection.execute('UPDATE warehouse__stock_item_holdings SET quantity_on_hand=2 WHERE stock_item_id=102')
        elif args.scenario == 'malformed-model':
            messages[-1] = {'content': '{'}
        run = make_pipeline(database, messages).run(DEMO_DATA / 'incoming.eml')
    # Stable identifiers make the checked-in synthetic sample directly comparable.
    run.run_id = f'synthetic-rfq-001-{args.scenario}'
    run.created_at = '2026-10-23T13:00:00+00:00'
    run.email.source_path = 'synthetic://incoming.eml'
    output = (args.output_dir or Path('runs/synthetic-demo') / args.scenario).resolve()
    output.mkdir(parents=True, exist_ok=True)
    (output / 'run.json').write_text(run.model_dump_json(indent=2) + '\n', encoding='utf-8')
    (output / 'proposal.md').write_text(
        '> OFFLINE REPLAY: authored model responses; all business data is synthetic.\n'
        '> Fixed demonstration timestamp; human review required.\n\n' + render_markdown(run),
        encoding='utf-8',
    )
    expected = {
        'standard': (Outcome.QUOTE_READY, 84.2, 6.32, 90.52),
        'shortage': (Outcome.INSUFFICIENT_STOCK, None, None, None),
        'malformed-model': (Outcome.PROCESSING_ERROR, None, None, None),
    }[args.scenario]
    actual = (run.result.outcome, run.result.subtotal, run.result.tax_total, run.result.total)
    if actual != expected:
        print(f'Demo failed: expected {expected}, got {actual}')
        return 1
    print('Offline synthetic RFQ replay (model responses are scripted; no network).')
    print('1. Parse incoming.eml for Cedar Workshop.')
    print('2. Validate scripted classification and extraction through the real agent classes.')
    print(f'3. Execute {len(run.tool_trace)} model-selected read-only tool calls.')
    print(f'4. Independently repeat {len(run.validation_trace)} deterministic lookups.')
    print('5. ' + ('Malformed JSON blocks processing before validation.' if args.scenario == 'malformed-model'
                   else 'Convert 2 Outer to 20 Each; validate stock, prices, tax, and quantities.'))
    print(f'6. Outcome: {run.result.outcome.value}; total: {run.result.total}; freight TBD.')
    print('7. Stop for human review. No outbound email or Teams post is made.')
    print(f'Proposal: {output / "proposal.md"}')
    print(f'Audit: {output / "run.json"}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
