import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent import Config, Store
from test_agent import SPACE, START, END, message
from test_review import PR
import review


SECRET = 'private-text-and-token-must-never-be-logged'


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = Config(SPACE, START)

    def records(self, logs):
        return [json.loads(row.getMessage()) for row in logs.records]

    def test_failed_read_identifies_job_stage_and_http_status_without_error_body(self):
        store = Store(self.root / 'state.sqlite3')
        self.addCleanup(store.db.close)
        store.ingest(self.config, [message()], START, END, None)
        job = store.claim()
        github = review.GitHub(self.config, self.root)

        def request(args, **kwargs):
            endpoint = args[-1]
            data = {'user': {'login': 'justinlee1253'},
                    'repos/ROKT/launch-engineering/pulls/6000': PR}
            if endpoint in data:
                return SimpleNamespace(returncode=0, stdout=json.dumps(data[endpoint]).encode())
            if '/reviews?' in endpoint:
                return SimpleNamespace(returncode=1, stdout=SECRET.encode(),
                    stderr=('gh: ' + SECRET + '\nHTTP 503').encode())
            raise AssertionError('Unexpected external call: ' + endpoint)

        with patch('review.subprocess.run', side_effect=request):
            with self.assertLogs('review-agent', level='WARNING') as logs:
                review.review_one(self.config, store, github, SimpleNamespace(), job)
        rows = self.records(logs)
        failed = next(row for row in rows if row['event'] == 'github_request_failed')
        self.assertEqual((failed['job_id'], failed['repo'], failed['pr']),
                         (job['id'], 'ROKT/launch-engineering', 6000))
        self.assertEqual((failed['stage'], failed['method'], failed['http_status']),
                         ('preflight', 'GET', 503))
        self.assertEqual(failed['endpoint'], '/repos/ROKT/launch-engineering/pulls/6000/reviews')
        self.assertEqual(failed['error_category'], 'server_error')
        self.assertGreaterEqual(failed['elapsed_ms'], 0)
        self.assertNotIn(SECRET, '\n'.join(logs.output))
        self.assertEqual(store.history()[0]['status'], 'needs-attention')

    def test_read_timeout_and_invalid_json_are_distinguishable(self):
        github = review.GitHub(self.config, self.root)
        cases = [(subprocess.TimeoutExpired('gh', 40, stderr=SECRET), 'timeout'),
                 (SimpleNamespace(returncode=0, stdout=SECRET.encode()), 'invalid_json')]
        for response, expected in cases:
            with self.subTest(expected=expected):
                options = {'side_effect': response} if isinstance(response, Exception) else {'return_value': response}
                with patch('review.subprocess.run', **options):
                    with self.assertLogs('review-agent', level='WARNING') as logs:
                        with self.assertRaises(review.GitHubError):
                            github.actor()
                self.assertEqual(self.records(logs)[0]['error_category'], expected)
                self.assertNotIn(SECRET, '\n'.join(logs.output))

    def test_successful_guard_reads_do_not_add_log_noise(self):
        github = review.GitHub(self.config, self.root)
        response = SimpleNamespace(returncode=0, stdout=b'{"login":"justinlee1253"}')
        with patch('review.subprocess.run', return_value=response):
            with self.assertNoLogs('review-agent', level='INFO'):
                self.assertEqual(github.actor(), 'justinlee1253')

    def test_ambiguous_write_keeps_reconciliation_semantics_and_hides_payload(self):
        github = review.GitHub(self.config, self.root)
        response = SimpleNamespace(returncode=1, stdout=SECRET.encode(), stderr=b'HTTP 502')
        with patch('review.subprocess.run', return_value=response):
            with self.assertLogs('review-agent', level='WARNING') as logs:
                with self.assertRaises(review.AmbiguousWrite):
                    github.api('repos/ROKT/launch-engineering/pulls/6000/reviews?access_token=' + SECRET, self.root / SECRET)
        row = self.records(logs)[0]
        self.assertEqual((row['method'], row['http_status']), ('POST', 502))
        self.assertNotIn(SECRET, '\n'.join(logs.output))

    def test_model_failures_keep_run_id_exit_code_and_safe_failure_category(self):
        cases = [('exit', 1, None, 'process_exit'),
                 ('missing', 0, None, 'missing_structured_output'),
                 ('invalid', 0, SECRET, 'invalid_structured_output')]
        for name, code, output, reason in cases:
            with self.subTest(name=name):
                reviewer = review.Reviewer(self.root)
                def process(args, **kwargs):
                    kwargs['stderr'].write(('HTTP 429: rate limit exceeded ' + SECRET).encode())
                    if output is not None:
                        Path(args[args.index('--output-last-message') + 1]).write_text(output)
                    return SimpleNamespace(returncode=code, poll=lambda: code)
                with patch('review.subprocess.Popen', side_effect=process):
                    with self.assertLogs('review-agent', level='INFO') as logs:
                        with self.assertRaises(review.Incomplete):
                            reviewer.invoke(SECRET, job='fixture-job')
                rows = self.records(logs)
                finished = next(row for row in rows if row['event'] == 'model_run_finished')
                self.assertEqual((finished['job_id'], finished['exit_code'], finished['failure_reason']),
                                 ('fixture-job', code, reason))
                self.assertEqual(finished['status'], 'failed')
                self.assertTrue(finished['run_id'])
                self.assertNotIn(SECRET, '\n'.join(logs.output))
                if name == 'exit':
                    self.assertEqual(finished['error_category'], 'rate_limited')

    def test_periodic_guard_failure_is_correlated_to_model_run_and_context_is_cleared(self):
        reviewer = review.Reviewer(self.root)
        github = review.GitHub(self.config, self.root)
        now = [0]
        checks = [0]
        child = SimpleNamespace(returncode=None, poll=lambda: child.returncode)
        def terminate():
            child.returncode = -15
        child.terminate = terminate
        child.wait = lambda **kwargs: child.returncode
        def guard():
            checks[0] += 1
            if checks[0] > 1:
                github.pr('ROKT/launch-engineering', 6000)
        def advance(_):
            now[0] += 16
        response = SimpleNamespace(returncode=1, stderr=b'HTTP 503', stdout=SECRET.encode())
        with patch('review.subprocess.Popen', return_value=child), patch('review.subprocess.run', return_value=response), patch('review.time.monotonic', side_effect=lambda: now[0]), patch('review.time.sleep', side_effect=advance):
            with self.assertLogs('review-agent', level='INFO') as logs:
                with self.assertRaises(review.GitHubError):
                    reviewer.invoke(SECRET, job='guard-job', guard=guard)
                with self.assertRaises(review.GitHubError):
                    github.actor()
        rows = self.records(logs)
        failed = next(row for row in rows if row['event'] == 'github_request_failed')
        finished = next(row for row in rows if row['event'] == 'model_run_finished')
        self.assertEqual((failed['job_id'], failed['stage']), ('guard-job', 'model'))
        self.assertEqual(failed['run_id'], finished['run_id'])
        self.assertEqual((finished['error_type'], finished['exit_code']), ('GitHubError', -15))
        self.assertNotIn('job_id', rows[-1])
        self.assertNotIn('run_id', rows[-1])
        self.assertNotIn(SECRET, '\n'.join(logs.output))


if __name__ == '__main__':
    unittest.main()
