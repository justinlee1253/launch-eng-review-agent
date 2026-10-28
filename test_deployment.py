"""Offline deployment regressions; never authenticate or start a live reviewer."""

import json
import os
import shutil
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import agent
import deployment
from test_agent import END, SPACE, START, message


class DeploymentFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.runtime = self.root / 'persistent'
        self.config = agent.Config(SPACE, START, worker_count=2)
        self.store = agent.Store(self.runtime / 'state.sqlite3')
        self.addCleanup(self.store.db.close)
        (self.runtime / 'config.json').write_text(json.dumps(asdict(self.config)))
        self.store.ingest(self.config, [message()], START, END, None)
        self.actual_host = deployment.host_id()
        host = patch('deployment.host_id', return_value='source')
        host.start()
        self.addCleanup(host.stop)

    def initialize(self):
        deployment.initialize(self.runtime, persistent=True, single_host=True)

    def healthy(self, at='2026-10-28T12:00:00Z'):
        with self.store.db:
            self.store._meta('heartbeat', at)
            self.store._meta('worker_count', 2)
        (self.runtime / 'auth-state.json').write_text(
            json.dumps({'status': 'ready', 'blocked': False})
        )


class DeploymentTests(DeploymentFixture):
    def test_existing_two_worker_configuration_loads(self):
        self.assertEqual(
            agent.Config.load(self.runtime / 'config.json').worker_count, 2
        )

    def test_runtime_override_is_shared_with_credentials(self):
        import auth

        with patch.dict(os.environ, {'REVIEW_AGENT_RUNTIME': str(self.runtime)}):
            self.assertEqual(deployment.runtime_path(self.root), self.runtime)
            manager = auth.CredentialManager(store=object())
            self.assertEqual(manager.directory, self.runtime)

    def test_missing_runtime_fails_without_creating_new_activation_or_queue(self):
        missing = self.root / 'lost-volume'
        with self.assertRaises(deployment.DeploymentError):
            deployment.verify(missing)
        self.assertFalse(missing.exists())
        (self.runtime / 'state.sqlite3').rename(self.runtime / 'state-saved.sqlite3')
        with self.assertRaises(deployment.DeploymentError):
            self.initialize()
        self.assertFalse((self.runtime / 'deployment.json').exists())

    def test_empty_database_is_rejected_without_reinitializing_it(self):
        self.initialize()
        self.store.db.close()
        database = self.runtime / 'state.sqlite3'
        database.write_bytes(b'')
        with self.assertRaises(deployment.DeploymentError):
            deployment.verify(self.runtime)
        self.assertEqual(database.stat().st_size, 0)

    def test_initialization_requires_explicit_storage_and_single_host_confirmation(
        self,
    ):
        for persistent, single_host in ((False, True), (True, False)):
            with self.assertRaises(deployment.DeploymentError):
                deployment.initialize(
                    self.runtime, persistent=persistent, single_host=single_host
                )
        self.assertFalse((self.runtime / 'deployment.json').exists())

    def test_copy_on_another_host_cannot_start_without_offline_handoff(self):
        self.initialize()
        with patch('deployment.host_id', return_value='other'):
            with self.assertRaises(deployment.DeploymentError):
                deployment.verify(self.runtime)
            with self.assertRaises(deployment.DeploymentError):
                deployment.initialize(self.runtime, persistent=True, single_host=True)

    def test_handoff_refuses_active_owner_and_never_signals_it(self):
        self.initialize()
        with (
            agent.process_lock(self.runtime / 'worker.lock'),
            self.assertRaises(deployment.DeploymentError),
        ):
            deployment.retire(self.runtime, 'target')
        deployment.verify(self.runtime)

    def test_retirement_blocks_source_and_preserves_queue_on_target(self):
        self.initialize()
        original = (self.runtime / 'config.json').read_bytes()
        deployment.retire(self.runtime, 'target')
        with self.assertRaises(deployment.DeploymentError):
            deployment.verify(self.runtime)
        self.store.db.close()
        target = self.root / 'target-volume'
        shutil.copytree(self.runtime, target)
        with (
            patch('deployment.host_id', return_value='wrong'),
            self.assertRaises(deployment.DeploymentError),
        ):
            deployment.activate(target)
        with patch('deployment.host_id', return_value='target'):
            deployment.activate(target)
            deployment.verify(target)
            self.assertEqual((target / 'config.json').read_bytes(), original)
            copied = agent.Store(target / 'state.sqlite3')
            try:
                self.assertEqual(len(copied.history()), 1)
                self.assertEqual(copied.history()[0]['status'], 'queued')
                self.assertEqual(copied.meta('checkpoint'), END)
                self.assertEqual(
                    copied.db.execute('select count(*) from outbox').fetchone()[0], 2
                )
            finally:
                copied.db.close()
        with self.assertRaises(deployment.DeploymentError):
            deployment.verify(self.runtime)

    def test_activation_rejects_tampered_identity_without_changing_the_marker(self):
        self.initialize()
        deployment.retire(self.runtime, 'target')
        marker = self.runtime / 'deployment.json'
        record = json.loads(marker.read_text())
        record['actor'] = 'wrong'
        marker.write_text(json.dumps(record))
        before = marker.read_bytes()
        with (
            patch('deployment.host_id', return_value='target'),
            self.assertRaises(deployment.DeploymentError),
        ):
            deployment.activate(self.runtime)
        self.assertEqual(marker.read_bytes(), before)

    def test_unplanned_replacement_requires_explicit_source_fencing(self):
        self.initialize()
        with patch('deployment.host_id', return_value='replacement'):
            with self.assertRaises(deployment.DeploymentError):
                deployment.activate(self.runtime)
            deployment.activate(self.runtime, source_fenced=True)
            deployment.verify(self.runtime)

    def test_health_rejects_missing_stale_future_paused_and_wrong_actor(self):
        self.initialize()
        now = agent.timestamp('2026-10-28T12:00:00Z').timestamp()
        with self.assertRaises(deployment.DeploymentError):
            deployment.health(self.runtime, 'justinlee1253', now)
        for at in ('2026-10-28T11:59:00Z', '2026-10-28T12:01:00Z'):
            self.healthy(at)
            with self.assertRaises(deployment.DeploymentError):
                deployment.health(self.runtime, 'justinlee1253', now)
        self.healthy()
        with self.assertRaises(deployment.DeploymentError):
            deployment.health(self.runtime, 'wrong', now)
        with self.store.db:
            self.store._meta('chat_blocked', 'paused')
        with self.assertRaises(deployment.DeploymentError):
            deployment.health(self.runtime, 'justinlee1253', now)
        with self.store.db:
            self.store._meta('chat_blocked', None)
        self.assertEqual(
            deployment.health(self.runtime, 'justinlee1253', now)['worker_count'], 2
        )
        (self.runtime / 'auth-state.json').write_text(
            '{"status":"blocked","blocked":true}'
        )
        with self.assertRaises(deployment.DeploymentError):
            deployment.health(self.runtime, 'justinlee1253', now)

    def test_retirement_racing_with_startup_is_checked_under_the_process_lock(self):
        self.initialize()
        original_guard = deployment.guard
        calls = []

        def guarded(directory, required=False):
            calls.append(1)
            original_guard(directory, required)
            if len(calls) == 1:
                deployment.retire(directory, 'target')

        with (
            patch('deployment.guard', side_effect=guarded),
            patch(
                'agent.Store',
                side_effect=AssertionError('retired runtime must not recover'),
            ),
            self.assertRaises(agent.ConfigError),
        ):
            agent.run(self.config, self.runtime)

    def test_status_does_not_create_a_database_after_volume_loss(self):
        import subprocess
        import sys

        self.initialize()
        self.store.db.close()
        (self.runtime / 'state.sqlite3').unlink()
        child = subprocess.run(
            [sys.executable, 'agent.py', 'status'],
            capture_output=True,
            check=False,
            text=True,
            env={**os.environ, 'REVIEW_AGENT_RUNTIME': str(self.runtime)},
        )
        self.assertNotEqual(child.returncode, 0)
        self.assertFalse((self.runtime / 'state.sqlite3').exists())

    def test_retired_runtime_is_rejected_before_worker_recovery(self):
        self.initialize()
        deployment.retire(self.runtime, 'target')
        with (
            patch(
                'agent.Store',
                side_effect=AssertionError('must not recover or mutate state'),
            ),
            self.assertRaises(agent.ConfigError),
        ):
            agent.run(self.config, self.runtime)


class EnsureScriptTests(DeploymentFixture):
    """Run the real shell entry point with fake external tools, never a live provider."""

    def script_fixture(self):
        import shlex
        import sys

        checkout = self.root / 'checkout'
        (checkout / 'deploy').mkdir(parents=True)
        shutil.copy('deploy/ensure.sh', checkout / 'deploy/ensure.sh')
        shutil.copy('deployment.py', checkout / 'deployment.py')
        (checkout / '.runtime').symlink_to(self.runtime, target_is_directory=True)
        (checkout / '.venv/bin').mkdir(parents=True)
        (checkout / '.venv/bin/python').write_text(
            '#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' "$@"\n'
        )
        (checkout / '.venv/bin/python').chmod(0o700)
        (checkout / 'review.py').write_text('def skill_text(): return "fixture"\n')
        (checkout / 'auth.py').write_text('import sys\nprint("{}")\n')
        (checkout / 'agent.py').write_text(
            'from pathlib import Path\nprint(Path("health-fixture.json").read_text())\n'
        )
        binary = checkout / 'bin'
        binary.mkdir()
        scripts = {
            'gh': 'case "$*" in "api user --jq .login") echo justinlee1253;; *) echo "[]";; esac',
            'codex': 'touch "$CODEX_CALLED"; exit 0',
            'pgrep': 'exit 0',
            'setsid': 'exit 97',
        }
        for name, source in scripts.items():
            path = binary / name
            path.write_text('#!/bin/sh\n' + source + '\n')
            path.chmod(0o700)
        with patch('deployment.host_id', return_value=self.actual_host):
            deployment.initialize(self.runtime, persistent=True, single_host=True)
        return checkout

    def execute(self, checkout, heartbeat=None, blocked=None):
        import subprocess

        (checkout / 'health-fixture.json').write_text(
            json.dumps(
                {
                    'space': SPACE,
                    'heartbeat': heartbeat,
                    'chat_blocked': blocked,
                    'google_auth': {'status': 'ready'},
                    'jobs': [],
                    'delivery': [],
                }
            )
        )
        return subprocess.run(
            ['bash', 'deploy/ensure.sh'],
            cwd=checkout,
            capture_output=True,
            check=False,
            text=True,
            timeout=20,
            env={
                **os.environ,
                'PATH': str(checkout / 'bin') + ':' + os.environ['PATH'],
                'REVIEW_AGENT_RUNTIME': str(self.runtime),
                'CODEX_CALLED': str(checkout / 'codex-called'),
            },
        )

    def test_ensure_exits_nonzero_for_missing_heartbeat_or_paused_chat(self):
        checkout = self.script_fixture()
        for blocked in (None, 'authorization_invalid'):
            with self.subTest(blocked=blocked):
                with self.store.db:
                    self.store._meta('heartbeat', None)
                    self.store._meta('chat_blocked', blocked)
                result = self.execute(checkout, blocked=blocked)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('Worker is unhealthy', result.stdout)

    def test_ensure_accepts_only_a_fresh_healthy_existing_runtime(self):
        checkout = self.script_fixture()
        self.healthy(agent.utc())
        result = self.execute(checkout, heartbeat=agent.utc())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('healthy: capacity=2', result.stdout)

    def test_ensure_rejects_actor_mismatch_before_codex_login(self):
        checkout = self.script_fixture()
        value = asdict(self.config)
        value['actor'] = 'other'
        value['operator_logins'] = ['other']
        (self.runtime / 'config.json').write_text(json.dumps(value))
        record = json.loads((self.runtime / 'deployment.json').read_text())
        record['actor'] = 'other'
        (self.runtime / 'deployment.json').write_text(json.dumps(record))
        result = self.execute(checkout)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((checkout / 'codex-called').exists())


if __name__ == '__main__':
    unittest.main()
