"""Explicit Google setup and silent renewal; grants live in macOS Keychain or an owner-only grant file."""

import argparse
import contextlib
import fcntl
import functools
import json
import logging
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
import requests
import deployment


# Both default to the pilot identity; a different deployment overrides them in its environment.
PROJECT_ID = os.environ.get('REVIEW_AGENT_GOOGLE_PROJECT') or 'le-orbit-review'
ACCOUNT = os.environ.get('REVIEW_AGENT_GOOGLE_ACCOUNT') or 'justin.lee@rokt.com'
GRANT_ENV = 'GOOGLE_CHAT_GRANT_JSON'
GRANT_FILE_ENV = 'REVIEW_AGENT_GRANT_FILE'
IDENTITY_SCOPE = 'openid'
USERINFO_URI = 'https://openidconnect.googleapis.com/v1/userinfo'
SERVICE = 'com.rokt.le-orbit-review.google-chat'
TOKEN_URI = 'https://oauth2.googleapis.com/token'
SCOPES = [
    'https://www.googleapis.com/auth/chat.messages.readonly',
    'https://www.googleapis.com/auth/chat.messages.create',
    'https://www.googleapis.com/auth/chat.messages.reactions.create',
]
REACTION_SCOPE = 'https://www.googleapis.com/auth/chat.messages.reactions'


def can_update_reactions(creds):
    scopes = creds.granted_scopes if creds.granted_scopes is not None else creds.scopes
    return bool({REACTION_SCOPE, 'https://www.googleapis.com/auth/chat.messages'} & set(scopes or []))


class AuthRequired(Exception):
    """Background work must pause; only deliberate setup can repair authorization."""


class TemporaryAuthError(Exception):
    """Transport/service problem; no consent or connection reset is warranted."""


class ConfigError(Exception):
    """Configuration or secure credential storage is unavailable."""


class KeychainStore:
    def __init__(self):
        if sys.platform != 'darwin':
            raise ConfigError('This setup requires macOS Keychain.')
        from keyring.backends.macOS import Keyring
        self.backend = Keyring()

    def get(self):
        try:
            return self.backend.get_password(SERVICE, ACCOUNT)
        except Exception:
            raise ConfigError('macOS Keychain could not be read; no login was started.') from None

    def set(self, value):
        try:
            self.backend.set_password(SERVICE, ACCOUNT, value)
            if self.backend.get_password(SERVICE, ACCOUNT) != value:
                raise ValueError('Keychain verification failed')
        except Exception:
            raise ConfigError('The grant could not be verified in macOS Keychain.') from None


class FileStore:
    """Owner-only grant file for hosts without Keychain; seeded once from an environment secret."""

    def __init__(self, path, seed_env=GRANT_ENV):
        self.path = Path(path)
        self.seed_env = seed_env

    def get(self):
        try:
            if self.path.exists():
                return self.path.read_text()
        except OSError:
            raise ConfigError('The grant file could not be read; no login was started.') from None
        seed = os.environ.get(self.seed_env, '').strip()
        if not seed:
            return None
        try:
            if not isinstance(json.loads(seed), dict):
                raise ValueError()
        except ValueError:
            raise ConfigError('The ' + self.seed_env + ' environment secret is not an exported grant.') from None
        self.set(seed)
        return seed

    def set(self, value):
        temp = None
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(dir=self.path.parent, prefix='.grant-')
            with os.fdopen(fd, 'w') as output:
                output.write(value)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temp, 0o600)
            os.replace(temp, self.path)
            temp = None
            if self.path.read_text() != value:
                raise ValueError('Grant file verification failed')
        except (OSError, ValueError):
            raise ConfigError('The grant could not be verified in the grant file.') from None
        finally:
            if temp and os.path.exists(temp):
                os.unlink(temp)


def default_store(directory):
    """Keychain on macOS; otherwise an owner-only file under the runtime directory."""
    override = os.environ.get(GRANT_FILE_ENV)
    if override:
        return FileStore(Path(override).expanduser())
    if sys.platform == 'darwin':
        return KeychainStore()
    return FileStore(Path(directory) / 'google-grant.json')


class CredentialManager:
    def __init__(self, store=None, state_dir=None, request_factory=None):
        self.directory = Path(state_dir) if state_dir else deployment.runtime_path(Path(__file__).parent)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        self.store = store if store is not None else default_store(self.directory)
        self.state_file = self.directory / 'auth-state.json'
        self.request_factory = request_factory or (lambda: functools.partial(Request(), timeout=20))

    @contextlib.contextmanager
    def _locked(self):
        fd = os.open(self.directory / 'auth.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def status(self):
        if not self.state_file.exists():
            return {'status': 'not_authorized', 'blocked': False}
        try:
            value = json.loads(self.state_file.read_text())
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, OSError):
            raise ConfigError('Local authentication state is invalid; no request was made.') from None

    def _state(self, status, *, reason=None, creds=None):
        now = datetime.now(timezone.utc).isoformat()
        state = {'status': status, 'blocked': status == 'blocked', 'updated_at': now}
        if reason:
            state['reason'] = reason
        if creds and creds.expiry:
            state['access_token_expires_at'] = creds.expiry.isoformat() + 'Z'
        if creds:
            state['reaction_updates_enabled'] = can_update_reactions(creds)
        if status == 'ready':
            state['refresh_verified_at'] = now
        fd, temp = tempfile.mkstemp(dir=self.directory, prefix='.auth-state-')
        try:
            with os.fdopen(fd, 'w') as output:
                json.dump(state, output, indent=2)
                output.write('\n')
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, self.state_file)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def _block(self, reason):
        self._state('blocked', reason=reason)
        raise AuthRequired('Google authorization needs repair. Background authentication is paused.') from None

    def _validate_grant(self, creds):
        if not creds.refresh_token:
            raise AuthRequired('No refresh token was received; unattended setup is incomplete.')
        granted = creds.granted_scopes if creds.granted_scopes is not None else creds.scopes
        if (not set(SCOPES[:2]).issubset(set(granted or []))
                or not (SCOPES[2] in set(granted or []) or can_update_reactions(creds))):
            raise AuthRequired('Not all three required Chat permissions were granted.')

    def _save(self, creds):
        self._validate_grant(creds)
        self.store.set(json.dumps({
            'project_id': PROJECT_ID,
            'credentials': json.loads(creds.to_json()),
        }))

    def load(self, *, force_refresh=False):
        """Never invokes an OAuth flow, even when no grant exists or refresh fails."""
        with self._locked():
            if self.status().get('blocked'):
                raise AuthRequired('Google authentication remains paused; explicit repair is required.')
            record = self.store.get()
            if not record:
                raise AuthRequired('No stored grant. Run the explicit authorize command once.')
            try:
                record = json.loads(record)
                info = record['credentials']
                if record['project_id'] != PROJECT_ID or info['token_uri'] != TOKEN_URI:
                    raise ValueError()
                creds = Credentials.from_authorized_user_info(info)
                self._validate_grant(creds)
            except (ValueError, KeyError, TypeError, AuthRequired):
                self._block('invalid_stored_grant')
            if force_refresh or not creds.valid:
                old_refresh = creds.refresh_token
                try:
                    creds.refresh(self.request_factory())
                except TransportError:
                    raise TemporaryAuthError('Google could not be reached; the grant was retained.') from None
                except RefreshError as error:
                    details = error.args[1] if len(error.args) > 1 and isinstance(error.args[1], dict) else {}
                    code = details.get('error')
                    if code in {'invalid_grant', 'invalid_client', 'unauthorized_client', 'access_denied', 'invalid_scope'}:
                        self._block(code)
                    raise TemporaryAuthError('Google renewal failed temporarily; the grant was retained.') from None
                # Refresh responses can omit a replacement refresh token.
                if not creds.refresh_token:
                    info = json.loads(creds.to_json())
                    info['refresh_token'] = old_refresh
                    creds = Credentials.from_authorized_user_info(info)
                try:
                    self._save(creds)
                except AuthRequired:
                    self._block('insufficient_scopes_after_refresh')
                self._state('ready', creds=creds)
            return creds

    def authorize(self, client_file, *, reauthorize=False, reaction_updates=False, flow_factory=None):
        """The only interactive entry point; called deliberately during setup."""
        try:
            if client_file is None and reaction_updates:
                record = json.loads(self.store.get() or '{}')
                info = record['credentials']
                config = {'installed': {'project_id': record['project_id'], 'client_id': info['client_id'],
                    'client_secret': info['client_secret'], 'token_uri': info['token_uri'],
                    'auth_uri': 'https://accounts.google.com/o/oauth2/auth'}}
            else:
                path = Path(client_file).expanduser()
                if path.stat().st_size > 65536:
                    raise ValueError()
                config = json.loads(path.read_text())
            client = config['installed']
            endpoint = urlsplit(client['auth_uri'])
            if (client['project_id'] != PROJECT_ID or not client.get('client_id')
                    or endpoint.scheme != 'https' or endpoint.netloc != 'accounts.google.com'
                    or client['token_uri'] != TOKEN_URI):
                raise ValueError()
        except (OSError, ValueError, KeyError, TypeError):
            raise ConfigError('Use the downloaded Desktop client JSON for le-orbit-review.') from None
        with self._locked():
            if self.store.get() and not reauthorize:
                raise AuthRequired('A grant already exists. Use check; explicit repair uses --reauthorize.')
            factory = flow_factory or InstalledAppFlow.from_client_config
            scopes = SCOPES[:2] + [REACTION_SCOPE] if reaction_updates else SCOPES
            flow = factory(config, scopes=scopes + [IDENTITY_SCOPE], autogenerate_code_verifier=True)
            # The SDK callback URL contains a short-lived code; keep it out of logs.
            logging.getLogger('google_auth_oauthlib').setLevel(logging.CRITICAL)
            try:
                creds = flow.run_local_server(
                    host='127.0.0.1', port=0, timeout_seconds=300,
                    open_browser=True, authorization_prompt_message=None,
                    success_message='Authorization received. Return to the terminal to verify storage and renewal.',
                    access_type='offline', prompt='consent',
                    login_hint=ACCOUNT, hd='rokt.com',
                )
            except Exception:
                raise AuthRequired('Initial authorization did not finish. No automatic login retry will occur.') from None
            self._validate_grant(creds)
            if reaction_updates:
                if not can_update_reactions(creds):
                    raise AuthRequired('Reaction replacement permission was not granted; the saved grant was retained.')
                try:
                    creds.refresh(self.request_factory())
                except (TransportError, RefreshError):
                    raise TemporaryAuthError('New grant renewal could not be verified; the saved grant was retained.') from None
                if not can_update_reactions(creds):
                    raise AuthRequired('Renewed grant lacks reaction replacement permission; the saved grant was retained.')
            self._save(creds)
            self._state('ready' if reaction_updates else 'authorized_refresh_unverified', creds=creds)

    def whoami(self):
        """Resolve the Chat user resource (users/ID) of the granted account; never opens a browser."""
        creds = self.load()
        if IDENTITY_SCOPE not in set(creds.granted_scopes or creds.scopes or []):
            raise AuthRequired('The grant has no identity scope; authorize again with --reauthorize before whoami.')
        try:
            response = requests.get(USERINFO_URI, headers={'Authorization': 'Bearer ' + creds.token}, timeout=20)
        except requests.RequestException:
            raise TemporaryAuthError('Google could not be reached; identity lookup skipped.') from None
        if response.status_code != 200:
            raise TemporaryAuthError('Identity lookup failed (HTTP ' + str(response.status_code) + ').')
        try:
            sub = str(response.json().get('sub', ''))
        except ValueError:
            sub = ''
        if not re.fullmatch(r'[0-9]{5,40}', sub):
            raise TemporaryAuthError('Identity lookup returned no user id.')
        return 'users/' + sub


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    setup = commands.add_parser('authorize', help='Explicit one-time browser authorization')
    setup.add_argument('--client-file')
    setup.add_argument('--reaction-updates', action='store_true', help='Explicit permission upgrade using the existing Keychain client; verifies renewal')
    setup.add_argument('--reauthorize', action='store_true', help='Explicitly replace an existing grant')
    check = commands.add_parser('check', help='Load the stored grant without any browser login')
    check.add_argument('--force-refresh', action='store_true')
    commands.add_parser('status', help='Show only non-secret local authentication health')
    commands.add_parser('whoami', help='Print the Chat user resource (users/ID) for configure --self-user')
    args = parser.parse_args()
    try:
        manager = CredentialManager()
        if args.command == 'authorize':
            if not args.client_file and not args.reaction_updates:
                raise ConfigError('Initial setup requires --client-file; --reaction-updates reuses an existing saved client.')
            print('Opening one Google authorization window. Choose ' + ACCOUNT + '.', flush=True)
            manager.authorize(args.client_file, reauthorize=args.reauthorize or args.reaction_updates,
                              reaction_updates=args.reaction_updates)
            print('Reaction permission and silent renewal verified in Keychain.' if args.reaction_updates else
                  'Grant verified in macOS Keychain. Run check --force-refresh to test renewal.', flush=True)
        elif args.command == 'check':
            manager.load(force_refresh=args.force_refresh)
            print('Stored grant loaded; required scopes present. No browser login was used.')
            print(json.dumps(manager.status(), indent=2))
        elif args.command == 'whoami':
            print(manager.whoami())
        else:
            print(json.dumps(manager.status(), indent=2))
        return 0
    except (AuthRequired, TemporaryAuthError, ConfigError) as error:
        print(str(error), file=sys.stderr)
        return 2 if isinstance(error, AuthRequired) else 1
    except KeyboardInterrupt:
        print('Authorization cancelled. No automatic retry will occur.', file=sys.stderr)
        return 130
    except Exception as error:
        # Avoid printing third-party exception text that can contain OAuth values.
        print('Authentication operation failed (' + type(error).__name__ + '); no credentials were printed.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
