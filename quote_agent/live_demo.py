"""Run synthetic RFQs against OpenRouter without any delivery adapters.

Only POST requests to the fixed OpenRouter completions endpoint are allowed.
Credentials are read at runtime from a local file and never included in output.
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from quote_agent.classifier import DEFAULT_CLASSIFIER_MODEL, RequestClassifier
from quote_agent.demo import DEMO_DATA, build_database
from quote_agent.handler import DEFAULT_HANDLER_MODEL, QuoteHandler
from quote_agent.models import Outcome
from quote_agent.openrouter import OPENROUTER_URL, OpenRouterClient, OpenRouterError, load_api_key
from quote_agent.pipeline import QuotePipeline
from quote_agent.render import render_markdown
from quote_agent.tools import QuoteTools


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise OpenRouterError('Redirect blocked in synthetic live demo')


class OpenRouterOnlyTransport:
    """Reject every network operation except one explicit model API endpoint."""
    def __init__(self, ca_bundle: str | None = None):
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca_bundle)),
            NoRedirect(),
        )
        self.requests = 0

    def __call__(self, request, *, timeout):
        if not isinstance(request, urllib.request.Request) or request.full_url != OPENROUTER_URL or request.get_method() != 'POST':
            raise OpenRouterError('Network action blocked: only OpenRouter completions are permitted')
        self.requests += 1
        return self.opener.open(request, timeout=timeout)


class RecordedClient(OpenRouterClient):
    def __init__(self, api_key: str):
        super().__init__(api_key)
        self.calls: list[dict] = []

    def chat(self, **kwargs):
        started = time.monotonic()
        metadata = {'requested_model': kwargs['model'], 'response_schema':(kwargs['response_model'].__name__ if kwargs.get('response_model') else 'tool_research')}
        print(f'OpenRouter call {len(self.calls)+1}: {metadata["response_schema"]} ({kwargs["model"]})', flush=True)
        try:
            reply = super().chat(**kwargs)
        except Exception as exc:
            # Provider errors may quote submitted data; redact the credential defensively.
            metadata['error'] = str(exc).replace(self._api_key, '[REDACTED]')[:2000]
            raise
        else:
            metadata.update({'returned_model':reply.model, 'usage':reply.usage, 'message':reply.message})
            return reply
        finally:
            metadata['elapsed_seconds'] = round(time.monotonic()-started, 3)
            self.calls.append(metadata)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--key-file', type=Path, required=True)
    parser.add_argument('--ca-bundle')
    parser.add_argument('--output-dir', type=Path, default=Path('runs/live-synthetic'))
    parser.add_argument('--classifier-model', default=DEFAULT_CLASSIFIER_MODEL)
    parser.add_argument('--handler-model', default=DEFAULT_HANDLER_MODEL)
    parser.add_argument('--scenario', choices=['standard','shortage'], default='standard')
    args = parser.parse_args(argv)
    # Explicit file wins for this command; environment credentials are not used.
    with patch.dict(os.environ, {'OPENROUTER_API_KEY':''}):
        key = load_api_key(args.key_file)
    client = RecordedClient(key)
    transport = OpenRouterOnlyTransport(args.ca_bundle)
    with TemporaryDirectory(prefix='live-synthetic-rfq-') as directory:
        database=Path(directory)/'synthetic.sqlite3'
        build_database(database)
        if args.scenario == 'shortage':
            with sqlite3.connect(database) as connection:
                connection.execute('UPDATE warehouse__stock_item_holdings SET quantity_on_hand=2 WHERE stock_item_id=102')
        tools = QuoteTools(database, DEMO_DATA/'reference')
        tools.price_source='Synthetic demonstration catalog'
        pipeline=QuotePipeline(RequestClassifier(client,model=args.classifier_model),
                               QuoteHandler(client,tools,model=args.handler_model),tools)
        # Model-selected tools are local read methods; Graph, SQL cloud and Teams
        # adapters are never constructed. urllib requests are additionally gated.
        with patch('urllib.request.urlopen', transport):
            run=pipeline.run(DEMO_DATA/'incoming.eml')
    run.email.source_path='synthetic://incoming.eml'
    output=args.output_dir.resolve()/run.run_id
    output.mkdir(parents=True,exist_ok=False)
    (output/'run.json').write_text(run.model_dump_json(indent=2)+'\n',encoding='utf-8')
    (output/'proposal.md').write_text(
        '> LIVE MODEL RUN via OpenRouter. Customer, products, stock, prices and tax are synthetic.\n'
        '> Local review only: no email or Teams delivery adapters are present.\n\n'+render_markdown(run),encoding='utf-8')
    metadata={'mode':'live_openrouter_synthetic_data','scenario':args.scenario,
              'network_policy':'POST https://openrouter.ai/api/v1/chat/completions only; redirects blocked',
              'request_count':transport.requests,'delivery_adapters_initialized':False,
              'credential_source':'explicit local key file (not copied to artifacts)', 'calls':client.calls}
    (output/'model_calls.json').write_text(json.dumps(metadata,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'outcome':run.result.outcome.value,'subtotal':run.result.subtotal,
                      'tax':run.result.tax_total,'total':run.result.total,
                      'agent_tool_calls':len(run.tool_trace),'validation_lookups':len(run.validation_trace),
                      'model_calls':len(client.calls),'proposal':str(output/'proposal.md'),
                      'audit':str(output/'run.json'),'model_responses':str(output/'model_calls.json')},indent=2))
    if run.result.outcome==Outcome.PROCESSING_ERROR:
        for call in client.calls:
            if 'error' in call:
                print('Provider diagnostic:',call['error'],file=sys.stderr)
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(main())
