import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from cryptography.fernet import Fernet
from starlette.testclient import TestClient

from chatgpt_channel.server import ChatGPT, create_app
from chatgpt_channel.store import Store
from chatgpt_channel.profile import registration, origin


class ChatGPTAccountsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'state.db'
        self.key = Fernet.generate_key().decode()
        self.environment = patch.dict('os.environ', {'CHATGPT_ALLOWED_API_ORIGINS': 'https://gateway.example.invalid'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.status = 200
        self.usage = {'remaining': '12.50', 'unit': 'USD', 'planName': 'Gateway pool'}
        self.requests = []
        def handle(request):
            self.requests.append(request)
            return httpx.Response(self.status, json=self.usage)
        self.http = httpx.Client(transport=httpx.MockTransport(handle))
        self.addCleanup(self.http.close)
        self.service = ChatGPT(Store(str(self.path), self.key), self.http)
        self.client = TestClient(create_app(self.service, 'm' * 40))
        self.addCleanup(self.client.close)
        self.headers = {'Authorization': 'Bearer ' + 'm' * 40, 'X-Organization-Id': 'org', 'X-User-Id': 'user'}
        self.payload = {'api_url': 'https://gateway.example.invalid/v1', 'api_key': 'synthetic-key-for-tests',
            'proxy': {'mode': 'direct'}, 'operation_id': 'create-one'}

    def create(self, registration=None):
        response = self.client.post('/v1/accounts', headers=self.headers,
            json=self.payload | ({'registration': registration} if registration else {}))
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_create_encrypted_without_membership_or_default(self):
        account = self.create()
        self.assertFalse(account['default'])
        self.assertEqual(account['registration'], {})
        self.assertIsNone(account['gateway_observation'])
        self.assertNotIn(self.payload['api_key'].encode(), self.path.read_bytes())
        self.assertEqual(self.requests, [])
        self.assertEqual(self.create()['account_id'], account['account_id'])

    def test_refresh_preserves_membership_and_failure_keeps_observation(self):
        account = self.create({'membership_tier': 'Pro', 'membership_expiry': '2026-11-14',
            'used_5h_percent': '25', 'used_7d_percent': '80'})
        url = '/v1/accounts/' + account['account_id'] + '/refresh'
        refreshed = self.client.post(url, headers=self.headers, json={}).json()['account']
        self.assertEqual(refreshed['registration'], account['registration'])
        self.assertEqual(refreshed['gateway_observation']['remaining'], '12.50')
        self.assertEqual(refreshed['gateway_observation']['planName'], 'Gateway pool')
        request = self.requests[-1]
        self.assertEqual(str(request.url), 'https://gateway.example.invalid/v1/usage')
        self.assertEqual(request.method, 'GET')
        self.status = 401
        failed = self.client.post(url, headers=self.headers, json={}).json()['account']
        self.assertEqual(failed['gateway_observation'], refreshed['gateway_observation'])
        self.assertEqual(failed['registration'], account['registration'])
        self.assertEqual(failed['authorization_state'], 'invalid')

    def test_organization_isolation_revision_and_archive(self):
        account = self.create()
        url = '/v1/accounts/' + account['account_id']
        self.assertEqual(self.client.get(url, headers=self.headers | {'X-Organization-Id': 'other'}).status_code, 404)
        disabled = self.client.patch(url, headers=self.headers, json={'expected_revision': 1, 'status': 'disabled'}).json()
        self.assertEqual(disabled['status'], 'disabled')
        conflict = self.client.patch(url, headers=self.headers, json={'expected_revision': 1, 'status': 'active'})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()['code'], 'REVISION_CONFLICT')
        archived = self.client.request(
            'DELETE', url, headers=self.headers, json={'expected_revision': disabled['revision']})
        self.assertEqual(archived.status_code, 200)
        self.assertEqual(self.service.accounts.list('org')['total'], 0)

    def test_validation_unknown_windows_and_endpoint_allowlist(self):
        self.assertIsNone(registration({'used_5h_percent': None})['used_5h_percent'])
        for value in ('-1', '101', 'NaN', True):
            with self.assertRaises(ValueError):
                registration({'used_7d_percent': value})
        with self.assertRaises(ValueError):
            registration({'reset_5h_at': '2026-10-16T10:00:00'})
        with self.assertRaises(ValueError):
            origin('https://api.openai.com/v1')
        for field in ('membership_expiry', 'reset_5h_at', 'reset_7d_at', 'observed_at'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                registration({field: False})

    def test_rotation_is_atomic_and_invalidates_old_gateway_observation(self):
        account = self.create({'membership_tier': 'Pro'})
        url = '/v1/accounts/' + account['account_id']
        current = self.client.post(url + '/refresh', headers=self.headers, json={}).json()['account']
        self.status = 401
        failed = self.client.put(url + '/credential', headers=self.headers, json={
            'api_key': 'synthetic-replacement-key', 'expected_revision': current['revision']})
        self.assertEqual(failed.status_code, 422)
        unchanged = self.service.accounts.get('org', account['account_id'])
        self.assertEqual(unchanged, current)
        self.status = 200
        result = self.client.put(url + '/credential', headers=self.headers, json={
            'api_key': 'synthetic-replacement-key', 'expected_revision': current['revision']})
        self.assertEqual(result.status_code, 200)
        changed = result.json()['account']
        self.assertIsNone(changed['gateway_observation'])
        self.assertEqual(changed['registration'], current['registration'])
        reopened = ChatGPT(Store(str(self.path), self.key), self.http)
        self.assertEqual(reopened.accounts.get('org', account['account_id']), changed)

    def test_empty_logs_and_no_execution_claim(self):
        logs = self.client.post('/v1/calls/search', headers=self.headers, json={})
        self.assertEqual(logs.status_code, 200)
        self.assertEqual(logs.json()['items'], [])
        self.assertEqual(self.client.get('/health').json()['execution_capabilities'], [])
