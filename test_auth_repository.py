"""Auth uses the existing bounded review pipeline with its own source guidance."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agent
import review
from test_agent import SPACE, START, END, message
from test_context import Remote
from test_review import BASE, HEAD, GitHub, Reviewer, result, snapshot


class AuthRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_auth_link_is_canonicalized_queued_and_deduplicated(self):
        config = agent.Config(SPACE, START)
        self.assertIn('ROKT/auth', config.context_repositories)
        store = agent.Store(self.root / 'state.sqlite3')
        self.addCleanup(store.db.close)
        for name in ('one', 'two'):
            store.ingest(config, [message(name, text='https://github.com/rokt/AUTH/pull/1945')], START, END, None)
        self.assertEqual([(r['repo'], r['number']) for r in store.history()], [('ROKT/auth', 1945)])
        self.assertEqual(agent.pr_links('https://github.com/ROKT/auth-evil/pull/1'), [])

    def test_auth_source_guidance_reaches_model_without_orbit_context(self):
        remote = Remote(self.root, path='internal/rebac/config/sats/prod/sats.yaml')
        for ref in (BASE, HEAD):
            remote.source[(ref, 'AGENTS.md')] = 'auth-guidance-' + ref
            remote.source[(ref, 'CLAUDE.md')] = '@AGENTS.md'
            remote.source[(ref, 'README.md')] = 'auth-overview-' + ref
            remote.source[(ref, 'docs/architecture.md')] = 'auth-architecture-' + ref
        evidence = remote.capture({'repo': 'ROKT/auth', 'number': 1945}, remote.data)
        evidence.number, evidence.job_id = 1945, 'auth-fixture'
        reviewer = review.Reviewer(self.root)
        prompts = []
        reviewer.invoke = lambda prompt, **kwargs: prompts.append(prompt) or {'status': 'incomplete', 'summary': 'fixture'}
        reviewer.review(remote, evidence)
        self.assertIn('auth-architecture-' + BASE, prompts[0])
        self.assertIn('auth-guidance-' + BASE, prompts[0])
        self.assertEqual(evidence.scope['profile'], 'auth')
        self.assertEqual(evidence.references, {})
        self.assertEqual(evidence.instructions, {})
        with self.assertRaises(review.Incomplete):
            evidence.fulfill(remote, [{'repo': 'ROKT/auth', 'ref': 'head', 'kind': 'context', 'path': 'wiki/orbit-overview.md'}])
        evidence.fulfill(remote, [{'repo': 'ROKT/auth', 'ref': 'main', 'kind': 'tree', 'path': 'internal/rebac'}])
        with self.assertRaises(review.Incomplete):
            evidence.fulfill(remote, [{'repo': 'ROKT/catalog', 'ref': 'main', 'kind': 'tree', 'path': ''}])

    def test_auth_review_uses_existing_verdict_and_ci_gates(self):
        blocker = {'blocking': True, 'severity': 'dangerous', 'path': 'handler.py', 'side': 'RIGHT', 'line': 1,
                   'title': 'Null user crashes handler', 'scenario': 'A lookup returns no user.',
                   'impact': 'Requests return 500.', 'evidence': 'return user.name',
                   'fix': 'Handle the missing user before accessing name.'}
        for ci, output, expected in [
                ('passed', result(), 'APPROVE'),
                ('passed', result(findings=[blocker]), 'REQUEST_CHANGES'),
                ('failed', result(), None),
                ('passed', result(status='incomplete'), None)]:
            with self.subTest(ci=ci, verdict=expected):
                store = agent.Store(self.root / (ci + output['status'] + str(expected)) / 'state.sqlite3')
                self.addCleanup(store.db.close)
                config = agent.Config(SPACE, START)
                store.ingest(config, [message(text='https://github.com/ROKT/auth/pull/1945')], START, END, None)
                job = store.claim()
                github = GitHub()
                github.ci = lambda *args: {'state': ci, 'checks': []}
                evidence = snapshot()
                evidence.repo = 'ROKT/auth'
                evidence.documents = {key.replace('ROKT/launch-engineering', 'ROKT/auth'): value
                                      for key, value in evidence.documents.items()}
                github.capture = lambda *args: evidence
                review.review_one(config, store, github, Reviewer(output), job)
                self.assertEqual([r['event'] for r in github.submitted], [expected] if expected else [])

    def test_auth_support_does_not_expand_stamp_permissions(self):
        config = agent.Config(SPACE, START)
        store = agent.Store(self.root / 'state.sqlite3')
        self.addCleanup(store.db.close)
        with patch.object(store, 'stamp_authorized', return_value=True):
            store.ingest(config, [message(text='https://github.com/ROKT/auth/pull/1945 STAMP')], START, END, None)
        self.assertEqual(store.history(), [])
        replies = [json.loads(row[0])['text'] for row in
                   store.db.execute("select payload from outbox where kind='message'")]
        self.assertTrue(any('STAMP' in text and 'ROKT/launch-engineering' in text for text in replies))

    def test_worker_rejects_a_previously_queued_auth_stamp(self):
        config = agent.Config(SPACE, START)
        store = agent.Store(self.root / 'state.sqlite3')
        self.addCleanup(store.db.close)
        store.ingest(config, [message(text='https://github.com/ROKT/auth/pull/1945')], START, END, None)
        job = store.claim()
        job['mode'] = 'stamp'
        github = GitHub()
        reviewer = Reviewer()
        with patch.object(reviewer, 'review') as analyze, patch.object(github, 'actor') as actor:
            review.review_one(config, store, github, reviewer, job)
        analyze.assert_not_called()
        actor.assert_not_called()
        self.assertEqual(github.submitted, [])
        self.assertEqual(store.history()[0]['status'], 'needs-attention')

    def test_explicit_context_scope_still_denies_auth(self):
        github = review.GitHub(agent.Config(SPACE, START, context_repositories=('ROKT/launch-engineering',)), self.root)
        with self.assertRaises(review.Incomplete):
            github.repo_path('ROKT/auth')
        with self.assertRaises(review.Incomplete):
            github.api('repos/ROKT/auth/pulls/1945')

    def test_evidence_schema_allows_auth_but_not_unapproved_repositories(self):
        schema = json.loads((agent.ROOT / 'review.schema.json').read_text())
        repos = schema['properties']['requests']['items']['properties']['repo']['enum']
        self.assertEqual(set(repos), {'ROKT/auth', 'ROKT/launch-engineering'})
        with self.assertRaises(agent.ConfigError):
            agent.Config(SPACE, START, context_repositories=('ROKT/catalog',))


if __name__ == '__main__':
    unittest.main()
