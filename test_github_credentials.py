import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent import Config
import review
from test_agent import SPACE, START


TOKEN = 'test-review-account-token'


class GitHubCredentialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.github = review.GitHub(Config(SPACE, START), Path(self.tmp.name))

    def test_reads_and_writes_keep_named_account_when_shared_account_changes(self):
        lookups, requests = [], []

        def run(args, **kwargs):
            if args[1:3] == ['auth', 'token']:
                lookups.append(args)
                self.assertEqual(args[-2:], ['--user', 'justinlee1253'])
                return SimpleNamespace(returncode=0, stdout=(TOKEN + '\n').encode(), stderr=b'')
            requests.append(kwargs['env'])
            if kwargs['env'].get('GH_TOKEN') != TOKEN:
                return SimpleNamespace(returncode=1, stdout=b'{}', stderr=b'HTTP 404')
            return SimpleNamespace(returncode=0, stdout=b'{"login":"justinlee1253"}', stderr=b'')

        with patch.dict(os.environ, {'GH_TOKEN': 'unrelated-shell-token', 'GITHUB_TOKEN': 'other-token'}), \
                patch('review.subprocess.run', side_effect=run):
            self.assertEqual(self.github.actor(), 'justinlee1253')
            os.environ['GH_TOKEN'] = 'changed-shell-token'
            self.github.api('repos/ROKT/launch-engineering/pulls/574', Path('payload.json'))
        self.assertEqual(len(lookups), 1)
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(env['GH_TOKEN'] == TOKEN for env in requests))
        self.assertNotIn(TOKEN, repr(lookups))

    def test_missing_named_credential_never_falls_back_or_sends_request(self):
        response = SimpleNamespace(returncode=1, stdout=b'', stderr=TOKEN.encode())
        with patch('review.subprocess.run', return_value=response) as run:
            with self.assertRaises(review.GitHubError) as caught:
                self.github.actor()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][1:3], ['auth', 'token'])
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_credential_lookup_failure_does_not_expose_output(self):
        with patch('review.subprocess.run', side_effect=subprocess.TimeoutExpired('gh', 40, output=TOKEN)):
            with self.assertRaises(review.GitHubError) as caught:
                self.github.actor()
        self.assertNotIn(TOKEN, str(caught.exception))


if __name__ == '__main__':
    unittest.main()
