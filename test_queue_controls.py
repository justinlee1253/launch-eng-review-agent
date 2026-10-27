import importlib.util
import json
import tempfile
import sys
import unittest
from pathlib import Path

from agent import Config, Store, utc
from test_agent import SPACE, START, END, message
from test_review import GitHub, Reviewer, result
import review


class QueueControlTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('queue_controls'), 'Queue controls have not been implemented.')
        import queue_controls
        self.controls = queue_controls
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = Path(self.tmp.name)
        self.store = Store(self.runtime / 'state.sqlite3')
        self.addCleanup(self.store.db.close)
        self.config = Config(SPACE, START)
        self.store.ingest(self.config, [message()], START, END, None)
        self.job = self.store.history()[0]

    def enable_stop(self):
        with self.store.db:
            self.store.event('worker_started')
            event = self.store.db.execute("select max(id) from events where code='worker_started'").fetchone()[0]
            self.store._meta('queue_controls', {'version': 1, 'started_event': event})

    def test_remove_keeps_history_and_cannot_be_claimed(self):
        self.controls.act(self.runtime, {'action': 'remove', 'job_id': self.job['id']})
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.store.history()[0]['status'], 'cancelled')
        self.assertEqual(self.store.db.execute('select count(*) from sources').fetchone()[0], 1)
        self.assertTrue(self.store.db.execute("select 1 from events where code='cancelled'").fetchone())
        self.assertTrue(self.store.db.execute("select 1 from outbox where kind='message' and payload like '%Cancelled%'").fetchone())

    def test_clear_only_removes_confirmed_waiting_requests(self):
        self.store.ingest(self.config, [message('other', text='https://github.com/ROKT/launch-engineering/pull/7000')], START, END, None)
        self.controls.act(self.runtime, {'action': 'clear', 'job_ids': [self.job['id']]})
        self.assertEqual(self.store.claim()['number'], 7000)

    def test_remove_cannot_silently_stop_a_review_that_started_after_page_refresh(self):
        self.store.claim()
        with self.assertRaises(self.controls.Conflict):
            self.controls.act(self.runtime, {'action': 'remove', 'job_id': self.job['id']})
        self.assertEqual(self.store.history()[0]['status'], 'reviewing')

    def test_active_stop_requires_updated_worker_and_survives_restart(self):
        self.store.claim()
        with self.assertRaises(self.controls.Conflict):
            self.controls.act(self.runtime, {'action': 'stop', 'job_id': self.job['id']})
        self.enable_stop()
        self.controls.act(self.runtime, {'action': 'stop', 'job_id': self.job['id']})
        self.store.recover()
        job = self.store.claim()
        github = GitHub()
        review.review_one(self.config, self.store, github, Reviewer(), job)
        self.assertEqual(self.store.history()[0]['status'], 'cancelled')
        self.assertEqual(github.submitted, [])

    def test_head_move_stays_owned_until_worker_finishes(self):
        job = self.store.claim()
        self.store.update(job, status='queued')
        with self.assertRaises(self.controls.Conflict):
            self.controls.act(self.runtime, {'action':'remove', 'job_id':job['id']})
        self.assertEqual(self.store.history()[0]['owner'], job['owner'])

    def test_cancel_wins_before_submission_reservation(self):
        self.enable_stop()
        job = self.store.claim()
        controls, runtime = self.controls, self.runtime
        class CancellingReviewer(Reviewer):
            def review(inner, *args):
                controls.act(runtime, {'action': 'stop', 'job_id': job['id']})
                return result()
        github = GitHub()
        review.review_one(self.config, self.store, github, CancellingReviewer(), job)
        self.assertEqual(self.store.history()[0]['status'], 'cancelled')
        self.assertEqual(github.submitted, [])

    def test_reserved_or_ambiguous_submission_cannot_be_cancelled(self):
        self.enable_stop()
        job = self.store.claim()
        self.assertTrue(self.store.reserve_submission(job, {'event': 'APPROVE'}, 'marker'))
        for status in ('submitting', 'ambiguous'):
            self.store.update(job, status=status)
            with self.assertRaises(self.controls.Conflict):
                self.controls.act(self.runtime, {'action': 'stop', 'job_id': job['id']})
        self.assertFalse(self.store.cancellation_requested(job))
        self.assertEqual(json.loads(self.store.history()[0]['payload'])['event'], 'APPROVE')

    def test_new_legacy_worker_generation_disables_active_stop(self):
        self.enable_stop()
        self.assertTrue(self.controls.supports_stop(self.store.db))
        with self.store.db:
            self.store.event('worker_started')
        self.assertFalse(self.controls.supports_stop(self.store.db))

    def test_stop_terminates_running_model_without_another_model_round(self):
        self.enable_stop()
        job = self.store.claim()
        marker = self.runtime / 'model-started'
        executable = self.runtime / 'fake-model'
        executable.write_text('#!' + sys.executable + '\nimport pathlib,time\npathlib.Path('
                              + repr(str(marker)) + ').touch()\ntime.sleep(30)\n')
        executable.chmod(0o700)
        reviewer = review.Reviewer(self.runtime)
        reviewer.executable = str(executable)
        def guard():
            if marker.exists():
                self.controls.act(self.runtime, {'action': 'stop', 'job_id': job['id']})
                raise self.controls.Cancelled()
        reviewer.cancel_guard = guard
        with self.assertRaises(self.controls.Cancelled):
            reviewer.invoke('fixture only', job=job['id'], timeout=2)
        run = self.store.db.execute('select * from model_runs').fetchone()
        self.assertEqual(run['status'], 'cancelled')
        self.assertIsNone(run['estimated_credits'])


if __name__ == '__main__':
    unittest.main()
