import importlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.auth.exceptions import TransportError
from google.oauth2.credentials import Credentials


SCOPES = [
    'https://www.googleapis.com/auth/chat.messages.readonly',
    'https://www.googleapis.com/auth/chat.messages.create',
    'https://www.googleapis.com/auth/chat.messages.reactions.create',
]


def credentials(*, refresh='FAKE-refresh', scopes=None, expired=True):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return Credentials(
        token='FAKE-access', refresh_token=refresh,
        token_uri='https://oauth2.googleapis.com/token',
        client_id='FAKE-client.apps.googleusercontent.com',
        client_secret='FAKE-client-secret', scopes=scopes or SCOPES,
        expiry=now + timedelta(hours=-2 if expired else 1),
    )


class MemoryStore:
    def __init__(self, creds=None):
        self.value = None if creds is None else json.dumps({
            'project_id': 'le-orbit-review',
            'credentials': json.loads(creds.to_json()),
        })

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class TokenEndpoint:
    def __init__(self, payload=None, status=200, error=None):
        self.payload = payload or {
            'access_token': 'FAKE-renewed-access', 'expires_in': 3600,
            'token_type': 'Bearer', 'scope': ' '.join(SCOPES),
        }
        self.status = status
        self.error = error
        self.calls = 0

    def __call__(self, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return type('Response', (), {
            'status': self.status,
            'data': json.dumps(self.payload).encode(),
            'headers': {'content-type': 'application/json'},
        })()


class AuthTests(unittest.TestCase):
    def setUp(self):
        try:
            self.auth = importlib.import_module('auth')
        except ModuleNotFoundError:
            self.fail('The local OAuth helper has not been implemented yet')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def manager(self, store=None, endpoint=None):
        return self.auth.CredentialManager(
            store=store if store is not None else MemoryStore(),
            state_dir=self.directory,
            request_factory=lambda: endpoint if endpoint is not None else TokenEndpoint(),
        )

    def client_file(self, **changes):
        client = {
            'project_id': 'le-orbit-review',
            'client_id': 'FAKE-client.apps.googleusercontent.com',
            'client_secret': 'FAKE-client-secret',
            'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
            'token_uri': 'https://oauth2.googleapis.com/token',
        }
        client.update(changes)
        path = self.directory / 'client.json'
        path.write_text(json.dumps({'installed': client}))
        return path

    def authorize_with(self, manager, creds):
        observed = {}

        def factory(config, scopes, **kwargs):
            observed.update({'scopes': scopes, **kwargs})

            class Flow:
                def run_local_server(self, **options):
                    observed.update(options)
                    return creds

            return Flow()

        manager.authorize(self.client_file(), flow_factory=factory)
        return observed

    def test_missing_grant_requires_explicit_setup(self):
        with self.assertRaises(self.auth.AuthRequired):
            self.manager().load()

    def test_expired_token_refreshes_and_preserves_refresh_grant(self):
        store = MemoryStore(credentials())
        renewed = self.manager(store).load()
        self.assertEqual(renewed.token, 'FAKE-renewed-access')
        self.assertEqual(renewed.refresh_token, 'FAKE-refresh')
        self.assertEqual(json.loads(store.value)['credentials']['refresh_token'], 'FAKE-refresh')
        state = json.loads((self.directory / 'auth-state.json').read_text())
        self.assertEqual(state['status'], 'ready')
        self.assertNotIn('FAKE-', json.dumps(state))

    def test_rotated_refresh_grant_is_persisted(self):
        endpoint = TokenEndpoint({
            'access_token': 'FAKE-rotated-access', 'refresh_token': 'FAKE-rotated-refresh',
            'expires_in': 3600, 'token_type': 'Bearer', 'scope': ' '.join(SCOPES),
        })
        store = MemoryStore(credentials())
        self.manager(store, endpoint).load()
        self.assertEqual(json.loads(store.value)['credentials']['refresh_token'], 'FAKE-rotated-refresh')

    def test_valid_grant_is_reused_after_manager_restart(self):
        store = MemoryStore(credentials())
        self.manager(store).load()
        unused_endpoint = TokenEndpoint(error=AssertionError('Unnecessary refresh'))
        reused = self.manager(store, unused_endpoint).load()
        self.assertEqual(reused.token, 'FAKE-renewed-access')
        self.assertEqual(unused_endpoint.calls, 0)

    def test_force_refresh_works_without_interactive_flow(self):
        store = MemoryStore(credentials(expired=False))
        endpoint = TokenEndpoint()
        renewed = self.manager(store, endpoint).load(force_refresh=True)
        self.assertEqual(renewed.token, 'FAKE-renewed-access')
        self.assertEqual(endpoint.calls, 1)

    def test_revoked_grant_blocks_later_attempts_without_secret_in_error(self):
        endpoint = TokenEndpoint({'error': 'invalid_grant', 'error_description': 'FAKE-secret'}, 400)
        manager = self.manager(MemoryStore(credentials()), endpoint)
        with self.assertRaises(self.auth.AuthRequired) as caught:
            manager.load()
        self.assertNotIn('FAKE-', str(caught.exception))
        self.assertTrue(json.loads((self.directory / 'auth-state.json').read_text())['blocked'])
        before = endpoint.calls
        with self.assertRaises(self.auth.AuthRequired):
            self.manager(manager.store, endpoint).load()
        self.assertEqual(endpoint.calls, before)

    def test_network_failure_does_not_mark_grant_revoked(self):
        manager = self.manager(MemoryStore(credentials()), TokenEndpoint(error=TransportError('FAKE-secret')))
        with self.assertRaises(self.auth.TemporaryAuthError) as caught:
            manager.load()
        self.assertNotIn('FAKE-', str(caught.exception))
        state = self.directory / 'auth-state.json'
        self.assertFalse(state.exists() and json.loads(state.read_text()).get('blocked', False))

    def test_missing_refresh_token_cannot_be_saved_as_success(self):
        store = MemoryStore()
        with self.assertRaises(self.auth.AuthRequired):
            self.authorize_with(self.manager(store), credentials(refresh=None, expired=False))
        self.assertIsNone(store.value)

    def test_incomplete_scope_grant_cannot_be_saved(self):
        store = MemoryStore()
        with self.assertRaises(self.auth.AuthRequired):
            self.authorize_with(self.manager(store), credentials(scopes=SCOPES[:1], expired=False))
        self.assertIsNone(store.value)

    def test_setup_requests_offline_pkce_and_exact_three_scopes(self):
        store = MemoryStore()
        observed = self.authorize_with(self.manager(store), credentials(expired=False))
        self.assertEqual(set(observed['scopes']), set(SCOPES))
        self.assertEqual(observed['access_type'], 'offline')
        self.assertTrue(observed['autogenerate_code_verifier'])
        self.assertEqual(observed['host'], '127.0.0.1')
        self.assertEqual(observed['login_hint'], 'justin.lee@rokt.com')
        self.assertIsNotNone(store.value)

    def test_setup_rejects_other_project_before_browser(self):
        path = self.client_file(project_id='unrelated-project')
        with self.assertRaises(self.auth.ConfigError):
            self.manager().authorize(path, flow_factory=lambda *a, **k: self.fail('Opened browser'))

    def test_setup_rejects_non_google_token_endpoint(self):
        path = self.client_file(token_uri='https://example.com/token')
        with self.assertRaises(self.auth.ConfigError):
            self.manager().authorize(path, flow_factory=lambda *a, **k: self.fail('Opened browser'))


if __name__ == '__main__':
    unittest.main()
