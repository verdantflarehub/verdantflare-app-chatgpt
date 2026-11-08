import base64
import io
import json
from email.parser import BytesParser
from email.policy import default
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
            if self.mode == 'sse_safety_refusal':
                tool_fail = {'item': {'type': 'image_generation_call', 'status': 'failed'}}
                msg = {'item': {'type': 'message', 'content': [{'type': 'output_text', 'text': '抱歉，我无法按原描述生成该图像。'}]}}
                done = {'type': 'response.completed', 'response': {'id': 'resp_safety_1', 'status': 'completed', 'output': [tool_fail['item'], msg['item']]}}
                return httpx.Response(200, content='event: response.output_item.done\ndata: ' + json.dumps(tool_fail) + '\n\n'
                    + 'event: response.output_item.done\ndata: ' + json.dumps(msg) + '\n\n'
                    + 'event: response.completed\ndata: ' + json.dumps(done) + '\n\ndata: [DONE]\n\n')
            if self.mode == 'sse_text_only_refusal':
                msg = {'item': {'type': 'message', 'content': [{'type': 'output_text', 'text': 'I cannot generate this image due to content policy.'}]}}
                done = {'type': 'response.completed', 'response': {'id': 'resp_refusal_2', 'status': 'completed', 'output': [msg['item']]}}
                return httpx.Response(200, content='event: response.output_item.done\ndata: ' + json.dumps(msg) + '\n\n'
                    + 'event: response.completed\ndata: ' + json.dumps(done) + '\n\ndata: [DONE]\n\n')
            if self.mode == 'sse_response_failed':
                failed_ev = {'type': 'response.failed', 'response': {'id': 'resp_fail_3', 'status': 'failed'}}
                return httpx.Response(200, content='event: response.failed\ndata: ' + json.dumps(failed_ev) + '\n\ndata: [DONE]\n\n')
            if self.mode == 'sse_empty_completed':
                done = {'type': 'response.completed', 'response': {'id': 'resp_empty_4', 'status': 'completed', 'output': []}}
                return httpx.Response(200, content='event: response.completed\ndata: ' + json.dumps(done) + '\n\ndata: [DONE]\n\n')
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
        self.assertEqual(result['concurrent_limit'], 4)
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
        sources = []
        for color in [(10, 20, 30), (40, 50, 60), (70, 80, 90)]:
            encoded = io.BytesIO()
            Image.new('RGB', (2, 3), color).save(encoded, format='PNG')
            sources.append({'mime_type': 'image/png', 'data': base64.b64encode(encoded.getvalue()).decode()})
        encoded_mask = io.BytesIO()
        Image.new('RGBA', (2, 3), (0, 0, 0, 0)).save(encoded_mask, format='PNG')
        mask = {'mime_type': 'image/png', 'data': base64.b64encode(encoded_mask.getvalue()).decode()}
        result = self.submit(payload={'prompt': 'edit', 'action': 'edit', 'images': sources,
            'mask': mask, 'quality': 'auto', 'prefer_4k': True}).json()
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['operation'], 'edit')
        request = self.requests[0]
        self.assertEqual(request.url.path, '/v1/images/edits')
        self.assertIn('multipart/form-data', request.headers['content-type'])
        message = BytesParser(policy=default).parsebytes(
            ('Content-Type: ' + request.headers['content-type'] + '\r\n\r\n').encode() + request.content)
        accepted, masks = [], []
        for part in message.iter_parts():
            name = part.get_param('name', header='content-disposition')
            if part.get_filename() and (name == 'image' or name.startswith('image[')):
                accepted.append(part.get_payload(decode=True))
            if name == 'mask':
                masks.append(part.get_payload(decode=True))
        self.assertEqual(accepted, [base64.b64decode(item['data']) for item in sources])
        self.assertEqual(masks, [encoded_mask.getvalue()])
        self.assertIn(b'3840x2160', request.content)
        self.assertNotIn(b'name="quality"', request.content)

    def test_single_image_edit_preserves_single_field(self):
        self.service.execution.prefer_responses = False
        self.submit(payload={'prompt': 'edit', 'action': 'edit', 'images': [REFERENCE]})
        request = self.requests[0]
        message = BytesParser(policy=default).parsebytes(
            ('Content-Type: ' + request.headers['content-type'] + '\r\n\r\n').encode() + request.content)
        files = [p for p in message.iter_parts() if p.get_filename()]
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].get_param('name', header='content-disposition'), 'image')
        self.assertEqual(files[0].get_payload(decode=True), base64.b64decode(IMAGE))

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

    def test_flare_model_supported_by_default(self):
        res = self.submit('flare-task', model_id='gpt-image-2.6-flare', payload={'prompt': 'concept sketch', 'complexity': 'medium'})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()['state'], 'completed')
        self.assertTrue(res.json()['accepted'])
        self.assertEqual(json.loads(self.requests[0].content)['complexity'], 'medium')
        legacy = self.submit('legacy-2-5-task', model_id='gpt-image-2.5-sunburst', payload={'prompt': 'legacy test'})
        self.assertEqual(legacy.status_code, 200, legacy.text)
        self.assertEqual(legacy.json()['state'], 'completed')

    def test_cleanup_unknown_releases_reservations_and_transitions_to_rejected(self):
        self.service.execution.prefer_responses = True
        self.mode = 'timeout'
        res = self.submit('zombie-1', payload={'prompt': 'test'}).json()
        self.assertEqual(res['state'], 'acceptance_unknown')
        self.assertEqual(self.service.calls.get('org', res['call_id'])['reservation_amount'], '2')
        cleanup = self.api.post('/v1/calls/cleanup-unknown', headers=self.headers, json={'operation_ids': ['zombie-1']})
        self.assertEqual(cleanup.status_code, 200, cleanup.text)
        data = cleanup.json()
        self.assertEqual(data['cleaned_count'], 1)
        self.assertEqual(data['rejected_operations'], ['zombie-1'])
        after = self.service.calls.get('org', res['call_id'])
        self.assertEqual(after['state'], 'rejected')
        self.assertEqual(after['accepted'], 0)
        self.assertEqual(after['reservation_amount'], '0')

    def test_sse_policy_refusal_and_terminal_failures_transition_to_failed_with_reason_code(self):
        self.service.execution.prefer_responses = True
        cases = [
            ('sse_safety_refusal', 'image_safety', 'resp_safety_1'),
            ('sse_text_only_refusal', 'image_safety', 'resp_refusal_2'),
            ('sse_response_failed', 'upstream_failed', 'resp_fail_3'),
            ('sse_empty_completed', 'no_image_returned', 'resp_empty_4'),
        ]
        for mode, expected_reason, expected_pid in cases:
            with self.subTest(mode=mode):
                self.mode = mode
                res = self.submit(f'op-{mode}', payload={'prompt': 'edit', 'action': 'edit', 'images': [REFERENCE]}).json()
                self.assertEqual(res['state'], 'failed')
                self.assertTrue(res['accepted'])
                self.assertEqual(res['reason_code'], expected_reason)
                row = self.service.calls.get('org', res['call_id'])
                self.assertEqual(row['state'], 'failed')
                self.assertEqual(row['accepted'], 1)
                self.assertEqual(row['reason_code'], expected_reason)
                self.assertEqual(row['provider_request_id'], expected_pid)
                # Idempotent re-read via POST /v1/calls or GET returns the same failed state without re-POSTing
                again = self.submit(f'op-{mode}', payload={'prompt': 'edit', 'action': 'edit', 'images': [REFERENCE]}).json()
                self.assertEqual(again['state'], 'failed')
                self.assertEqual(again['reason_code'], expected_reason)
