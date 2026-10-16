import base64
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
import httpx
from PIL import Image
from starlette.testclient import TestClient

from chatgpt_channel.execution import MODEL
from chatgpt_channel.server import ChatGPT, create_execution
from chatgpt_channel.store import Store

buffer = io.BytesIO()
Image.new('RGB', (2, 3), (20, 40, 60)).save(buffer, format='PNG')
IMAGE = base64.b64encode(buffer.getvalue()).decode()
REFERENCE = {'mime_type': 'image/png', 'data': IMAGE}


class ExecutionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.db'
        self.key = Fernet.generate_key().decode()
        env = patch.dict('os.environ', {'CHATGPT_ALLOWED_API_ORIGINS': 'https://gateway.example.invalid'})
        env.start()
        self.addCleanup(env.stop)
        self.requests, self.mode = [], 'success'
        def upstream(request):
            self.requests.append(request)
            if self.mode == 'timeout':
                raise httpx.ReadTimeout('synthetic interruption')
            if self.mode in {'400', '429', '500'}:
                return httpx.Response(int(self.mode))
            if self.mode == 'partial':
                data = {'type': 'response.image_generation_call.partial_image', 'partial_image_b64': IMAGE}
                return httpx.Response(200, content='data: ' + json.dumps(data) + '\n\ndata: [DONE]\n\n')
            if self.mode == 'sse':
                partial = {'type': 'response.image_generation_call.partial_image', 'partial_image_b64': 'not-final'}
                final = {'item': {'type': 'image_generation_call', 'status': 'completed', 'result': IMAGE}}
                return httpx.Response(200, content='data: ' + json.dumps(partial) + '\n\n'
                    + 'event: response.output_item.done\ndata: ' + json.dumps(final) + '\n\ndata: [DONE]\n\n')
            return httpx.Response(200, json={'data': [{'b64_json': IMAGE if self.mode == 'success' else 'malformed'}]})
        self.http = httpx.Client(transport=httpx.MockTransport(upstream))
        self.addCleanup(self.http.close)
        self.service = ChatGPT(Store(str(self.path), self.key), self.http)
        account = self.service.accounts.create('org', 'admin', 'create', {
            'api_url': 'https://gateway.example.invalid/v1', 'api_key': 'synthetic-test-key',
            'proxy': {'mode': 'direct'}, 'default': True, 'registration': {'budget_limit': '20'}})
        self.account = self.service.accounts.checked('org', 'admin', account['account_id'], 1, 'active')
        self.api = TestClient(create_execution(self.service, 'e' * 40))
        self.addCleanup(self.api.close)
        self.headers = {'Authorization': 'Bearer ' + 'e' * 40, 'X-Organization-Id': 'org', 'X-Caller-Id': 'image-mcp'}

    def submit(self, operation='task', **changes):
        return self.api.post('/v1/calls', headers=self.headers, json={
            'idempotency_key': operation, 'task_id': operation, 'model_id': MODEL, 'capability': 'image',
            'payload': {'prompt': 'synthetic test'}, 'budget_limit': '2', **changes})

    def test_generation_4k_parameters_and_encrypted_result_after_archival(self):
        response = self.submit(payload={'prompt': 'synthetic test', 'prefer_4k': True, 'size': '1152x2048',
                                        'quality': 'hd', 'background': 'transparent'})
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result['state'], 'completed')
        self.assertIsNone(result['budget']['amount'])
        self.assertIsNone(result['settlement']['amount'])
        self.assertEqual(self.requests[0].url.path, '/v1/images/generations')
        self.assertEqual(json.loads(self.requests[0].content), {'model': MODEL, 'prompt': 'synthetic test',
            'size': '2160x3840', 'quality': 'high', 'background': 'transparent', 'response_format': 'b64_json'})
        self.service.accounts.patch('org', 'admin', self.account['account_id'], self.account['revision'], {}, archive=True)
        output = self.api.get('/v1/calls/' + result['call_id'] + '/result', headers=self.headers).json()
        self.assertEqual(output['image']['data'], IMAGE)
        self.assertNotIn(IMAGE.encode(), self.path.read_bytes())

    def test_multipart_multiple_references_and_mask_preserve_edit_route(self):
        self.service.execution.prefer_responses = True
        result = self.submit(payload={'prompt': 'edit', 'action': 'edit', 'images': [REFERENCE, REFERENCE],
            'mask': REFERENCE, 'quality': 'auto', 'prefer_4k': True}).json()
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['operation'], 'edit')
        request = self.requests[0]
        self.assertEqual(request.url.path, '/v1/images/edits')
        self.assertIn('multipart/form-data', request.headers['content-type'])
        for field in (b'name="image"', b'name="image_1"', b'name="mask"', b'3840x2160'):
            self.assertIn(field, request.content)
        self.assertNotIn(b'name="quality"', request.content)

    def test_responses_preserves_instructions_and_uses_only_final_image(self):
        self.service.execution.prefer_responses = True
        self.mode = 'sse'
        result = self.submit(payload={'prompt': 'edit', 'action': 'edit', 'images': [REFERENCE],
                                     'quality': 'auto', 'instructions': 'Synthetic explicit instructions'}).json()
        self.assertEqual(result['state'], 'completed')
        request = self.requests[0]
        body = json.loads(request.content)
        self.assertEqual(request.url.path, '/v1/responses')
        self.assertTrue(body['stream'])
        self.assertEqual(body['instructions'], 'Synthetic explicit instructions')
        self.assertEqual(body['tools'][0]['action'], 'edit')
        self.assertEqual(body['tools'][0]['quality'], 'high')
        self.assertEqual(body['input'][0]['content'][0]['image_url'], 'data:image/png;base64,' + IMAGE)

    def test_partial_stream_timeout_and_http_errors_never_fallback_or_resubmit(self):
        self.service.execution.prefer_responses = True
        for mode in ('partial', 'timeout', '429', '500', 'bad'):
            self.mode = mode
            result = self.submit(mode, payload={'prompt': 'test', 'prefer_4k': True}).json()
            self.assertEqual(result['state'], 'acceptance_unknown')
            self.assertIsNone(result['accepted'])
            self.assertEqual(self.submit(mode, payload={'prompt': 'test', 'prefer_4k': True}).json(), result)
            reopened = ChatGPT(Store(str(self.path), self.key), self.http)
            self.assertEqual(reopened.execution.submit('org', 'image-mcp', mode, mode, MODEL,
                {'prompt': 'test', 'prefer_4k': True}, None, '2'), result)
        self.assertEqual(len(self.requests), 5)
        self.assertTrue(all(request.url.path == '/v1/responses' for request in self.requests))

    def test_explicit_rejection_releases_reservation_without_retry(self):
        self.mode = '400'
        result = self.submit().json()
        self.assertEqual(result['state'], 'rejected')
        self.assertFalse(result['accepted'])
        self.assertEqual(self.service.calls.get('org', result['call_id'])['reservation_amount'], '0')
        self.assertEqual(self.submit().json(), result)
        self.assertEqual(len(self.requests), 1)

    def test_invalid_inputs_and_unauthorized_callers_never_reach_gateway(self):
        self.assertEqual(self.api.post('/v1/calls').status_code, 401)
        for changes in ({'model_id': 'unapproved'}, {'budget_limit': None}, {'capability': 'video'},
                {'payload': {'prompt': 'test', 'action': 'edit', 'images': [{'url': 'https://unapproved.invalid'}]}},
                {'payload': {'prompt': 'test', 'size': '99999x99999'}},
                {'payload': {'prompt': 'test', 'api_url': 'https://unapproved.invalid'}}):
            self.assertGreaterEqual(self.submit(**changes).status_code, 400)
        self.assertEqual(self.requests, [])
        result = self.submit().json()
        foreign = {**self.headers, 'X-Organization-Id': 'other'}
        self.assertEqual(self.api.get('/v1/calls/' + result['call_id'] + '/result', headers=foreign).status_code, 404)
        wrong = {**self.headers, 'X-Caller-Id': 'video-mcp'}
        self.assertEqual(self.api.get('/v1/calls/' + result['call_id'], headers=wrong).status_code, 403)
