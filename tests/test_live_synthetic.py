"""No-network tests for the live-model entry point and research finalization."""
import io
import json
import os
import unittest
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from quote_agent.demo import DEMO_DATA, ReplayClient, build_database
from quote_agent.handler import QuoteHandler
from quote_agent.ingestion import parse_eml
from quote_agent.live_demo import NoRedirect, OpenRouterOnlyTransport, main
from quote_agent.openrouter import OPENROUTER_URL, OpenRouterClient, OpenRouterError, OpenRouterReply
from quote_agent.tools import QuoteTools
from quote_agent.models import HandlerDraft
from quote_agent.openrouter import _strict_response_schema


class LiveSyntheticTests(unittest.TestCase):
    def test_model_schema_requires_explicit_line_items_and_nested_fields(self):
        original=HandlerDraft.model_json_schema()
        strict=_strict_response_schema(original)
        self.assertEqual(set(strict['required']),set(strict['properties']))
        line=strict['$defs']['RequestedLine']
        self.assertEqual(set(line['required']),set(line['properties']))
        self.assertNotIn('default',strict['properties']['customer_hint'])
        # Changing the API schema does not remove local application defaults.
        self.assertEqual(HandlerDraft(summary='none',suggested_response='review').lines,[])

    def test_only_completion_posts_are_allowed(self):
        transport = OpenRouterOnlyTransport()
        transport.opener = Mock()
        for url, method in [('https://graph.microsoft.com/v1.0/me/sendMail','POST'),
                            ('https://sample.example/teams','POST'), (OPENROUTER_URL,'GET')]:
            with self.subTest(url=url, method=method), self.assertRaises(OpenRouterError):
                transport(urllib.request.Request(url,method=method),timeout=1)
        transport.opener.open.assert_not_called()
        request=urllib.request.Request(OPENROUTER_URL,method='POST')
        transport(request,timeout=1)
        transport.opener.open.assert_called_once_with(request,timeout=1)
        self.assertEqual(transport.requests,1)
        with self.assertRaises(OpenRouterError):
            NoRedirect().redirect_request(None)

    def test_partial_progress_response_requires_final_extraction(self):
        messages=json.loads((DEMO_DATA/'model_messages.json').read_text())['messages']
        progress={'content':json.dumps({'summary':'I will research this request','suggested_response':'Working'})}
        client=ReplayClient([progress,messages[-1]])
        with TemporaryDirectory() as directory:
            db=Path(directory)/'fixture.sqlite3'
            build_database(db)
            handler=QuoteHandler(client,QuoteTools(db,DEMO_DATA/'reference'))
            draft, trace=handler.handle(parse_eml(DEMO_DATA/'incoming.eml'))
        self.assertEqual(len(draft.lines),2)
        self.assertEqual(trace,[])

    def test_live_entry_uses_explicit_key_and_synthetic_tools_without_network(self):
        messages=json.loads((DEMO_DATA/'model_messages.json').read_text())['messages']
        replies=[OpenRouterReply(message=m,model='mocked-live-model',usage={}) for m in messages]
        with TemporaryDirectory() as directory:
            root=Path(directory)
            key=root/'key.txt'
            key.write_text('sk-or-v1-'+'a'*64)
            key.chmod(0o600)
            with patch.dict(os.environ,{'OPENROUTER_API_KEY':'invalid-env-key'}), \
                    patch.object(OpenRouterClient,'chat',side_effect=replies) as chat, \
                    patch('socket.socket.connect',side_effect=AssertionError('network forbidden')), \
                    redirect_stdout(io.StringIO()):
                result=main(['--key-file',str(key),'--output-dir',str(root/'runs')])
            self.assertEqual(result,0)
            output=next((root/'runs').iterdir())
            record=json.loads((output/'run.json').read_text())
            metadata=json.loads((output/'model_calls.json').read_text())
            self.assertEqual(record['result']['total'],90.52)
            self.assertFalse(metadata['delivery_adapters_initialized'])
            self.assertEqual(metadata['request_count'],0)
            self.assertNotIn(key.read_text(),(output/'model_calls.json').read_text())
            self.assertIsNone(chat.call_args_list[1].kwargs['response_model'])


if __name__=='__main__':
    unittest.main()
