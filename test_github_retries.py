from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent import Config
import review
from test_agent import SPACE, START


OK = SimpleNamespace(returncode=0, stdout=b'{"login":"justinlee1253"}', stderr=b'')


class GitHubRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.github = review.GitHub(Config(SPACE, START), Path(self.tmp.name))
        transport = patch.object(review.GitHub, 'environment', return_value={})
        transport.start()
        self.addCleanup(transport.stop)

    def test_transient_read_failure_recovers_without_aborting_review(self):
        for failure in [SimpleNamespace(returncode=1, stdout=b'', stderr=b'error connecting to api.github.com'),
                        SimpleNamespace(returncode=1, stdout=b'', stderr=b'HTTP 503'),
                        subprocess.TimeoutExpired('gh', 40)]:
            with self.subTest(failure=failure), patch('review.subprocess.run', side_effect=[failure, OK]) as run, \
                    patch('review.time.sleep') as sleep:
                self.assertEqual(self.github.actor(), 'justinlee1253')
                self.assertEqual(run.call_count, 2)
                sleep.assert_called_once_with(1)

    def test_retries_are_bounded(self):
        failure = SimpleNamespace(returncode=1, stdout=b'', stderr=b'error connecting to api.github.com')
        with patch('review.subprocess.run', return_value=failure) as run, patch('review.time.sleep'):
            with self.assertRaises(review.GitHubError): self.github.actor()
        self.assertEqual(run.call_count, 3)

    def test_permission_and_missing_resource_reads_are_not_retried(self):
        for code, error in [(401, review.GitHubError), (403, review.GitHubError), (404, FileNotFoundError)]:
            with self.subTest(code=code), patch('review.subprocess.run', return_value=SimpleNamespace(
                    returncode=1, stdout=b'', stderr=f'HTTP {code}'.encode())) as run, patch('review.time.sleep') as sleep:
                with self.assertRaises(error): self.github.actor()
                self.assertEqual(run.call_count, 1)
                sleep.assert_not_called()

    def test_uncertain_write_is_never_retried(self):
        for failure in [SimpleNamespace(returncode=1, stdout=b'', stderr=b'HTTP 503'),
                        subprocess.TimeoutExpired('gh', 40)]:
            with self.subTest(failure=failure), patch('review.subprocess.run', side_effect=[failure]) as run, \
                    patch('review.time.sleep') as sleep:
                with self.assertRaises(review.AmbiguousWrite):
                    self.github.api('repos/ROKT/launch-engineering/pulls/574/reviews', Path('payload.json'))
                self.assertEqual(run.call_count, 1)
                sleep.assert_not_called()

    def test_retry_keeps_the_overall_deadline(self):
        now = [0]
        with patch('review.time.monotonic', side_effect=lambda: now[0]):
            self.github.budget = review.ReviewBudget()
            now[0] = 1199
            def wait(seconds):
                now[0] += seconds
            failure = SimpleNamespace(returncode=1, stdout=b'', stderr=b'HTTP 503')
            with patch('review.subprocess.run', return_value=failure) as run, patch('review.time.sleep', side_effect=wait):
                with self.assertRaises(review.ReviewTimedOut): self.github.actor()
            self.assertEqual(run.call_count, 1)

    def test_cancellation_is_checked_before_retry(self):
        failure = SimpleNamespace(returncode=1, stdout=b'', stderr=b'HTTP 503')
        self.github.cancel_guard = lambda: None
        def cancel_after_wait(seconds):
            def cancelled(): raise review.WorkerStopped('cancelled')
            self.github.cancel_guard = cancelled
        with patch('review.subprocess.run', return_value=failure) as run, patch('review.time.sleep', side_effect=cancel_after_wait):
            with self.assertRaises(review.WorkerStopped): self.github.actor()
        self.assertEqual(run.call_count, 1)

    def test_retry_wait_respects_shutdown(self):
        from threading import Event
        stop = Event()
        self.github.stop = stop
        failure = SimpleNamespace(returncode=1, stdout=b'', stderr=b'HTTP 503')
        with patch('review.subprocess.run', return_value=failure) as run, patch.object(stop, 'wait', side_effect=lambda _: stop.set()):
            with self.assertRaises(review.WorkerStopped): self.github.actor()
        self.assertEqual(run.call_count, 1)


if __name__ == '__main__': unittest.main()
