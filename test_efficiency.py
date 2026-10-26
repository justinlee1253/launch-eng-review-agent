"""Focused checks for spending, queue behavior, and the approval trust boundary."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent import Config, REACTIONS, Store, flush_outbox
from test_agent import SPACE, START, END, message
from test_review import GitHub, HEAD
import review
import usage


class NoModel:
    def review(self, *args):
        raise AssertionError('This request must not spend model tokens')


class EfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'state.sqlite3')
        self.addCleanup(self.store.db.close)
        self.config = Config(SPACE, START)
        self.github = GitHub()
        self.github.ci = lambda *args: {'state': 'passed', 'checks': ['buildkite/pipeline']}

    def enqueue(self, msg=None):
        self.store.ingest(self.config, [msg or message()], START, END, None)
        return self.store.claim()

    def test_credit_estimate_counts_cached_input_once_and_reasoning_as_output(self):
        tokens = {'input_tokens': 1_000_000, 'cached_input_tokens': 200_000,
                  'output_tokens': 100_000, 'reasoning_output_tokens': 80_000}
        self.assertEqual(usage.estimate_credits(tokens), 131.0)
        self.assertIsNone(usage.estimate_credits(None))

    def test_failed_invocations_retain_reported_cost_and_unreported_usage_stays_unknown(self):
        run = self.store.start_model_run(None)
        self.store.end_model_run(run, 'failed', {'input_tokens': 1000, 'cached_input_tokens': 0,
            'output_tokens': 100, 'reasoning_output_tokens': 50})
        other = self.store.start_model_run(None)
        self.store.end_model_run(other, 'cancelled', None)
        report = self.store.costs()
        self.assertAlmostEqual(report['known_estimated_credits'], 0.15)
        self.assertEqual(report['unknown_usage_runs'], 1)
        self.assertIsNone(report['actual_usd'])

    def test_duplicate_reply_points_to_first_request_time_without_another_job(self):
        self.enqueue()
        duplicate = message('second', createTime='2026-10-26T18:53:00Z',
                            sender={'name': 'users/someoneelse', 'type': 'HUMAN'})
        self.store.ingest(self.config, [duplicate], START, END, None)
        self.assertEqual(len(self.store.history()), 1)
        replies = [json.loads(row[0])['text'] for row in self.store.db.execute("select payload from outbox where kind='message'")]
        self.assertTrue(any('18:52:00' in text and 'Already requested' in text for text in replies))

    def test_fifo_uses_original_request_time_even_after_job_is_updated(self):
        first = self.enqueue()
        later = message('later', text='https://github.com/ROKT/launch-engineering/pull/6001', createTime='2026-10-26T18:53:00Z')
        self.store.ingest(self.config, [later], START, END, None)
        self.store.update(first, status='queued')
        self.assertEqual(self.store.claim()['number'], 6000)

    def test_failed_pending_conflicted_and_draft_prs_do_not_start_the_model(self):
        for setting in ('failed', 'pending', 'conflicted', 'draft'):
            with self.subTest(setting=setting):
                msg = message(setting, text='https://github.com/ROKT/launch-engineering/pull/' + str(6100 + len(self.store.history())))
                job = self.enqueue(msg)
                self.github.data['draft'] = setting == 'draft'
                self.github.data['mergeable_state'] = 'dirty' if setting == 'conflicted' else 'clean'
                self.github.ci = lambda *args: {'state': setting if setting in {'failed', 'pending'} else 'passed', 'checks': ['buildkite/pipeline']}
                review.review_one(self.config, self.store, self.github, NoModel(), job)
                self.assertEqual(self.github.submitted, [])

    def test_only_verified_mani_stamp_approves_without_reading_diff_or_starting_model(self):
        msg = message(text='https://github.com/ROKT/launch-engineering/pull/6000 STAMP',
                      sender={'name': 'users/123456789', 'email': 'mani.vedma@rokt.com', 'type': 'HUMAN'})
        job = self.enqueue(msg)
        self.github.capture = lambda *args: self.fail('Stamp must not read the diff')
        review.review_one(self.config, self.store, self.github, NoModel(), job)
        self.assertEqual(len(self.github.submitted), 1)
        self.assertEqual(self.github.submitted[0]['event'], 'APPROVE')
        self.assertIn('without code review', self.github.submitted[0]['body'])
        self.assertEqual(self.store.history()[0]['mode'], 'stamp')
        result = self.store.history()[0]['result']
        self.assertIn('Stamped — approved as requested without code review.', result)
        self.assertIn('head ' + HEAD[:8], result)

    def test_stamp_email_in_message_or_display_name_cannot_grant_permission(self):
        for sender in ({'name': 'users/other', 'email': 'other@rokt.com', 'displayName': 'Mani Vedma', 'type': 'HUMAN'},
                       {'name': 'users/unknown', 'type': 'HUMAN'}):
            msg = message(sender['name'].split('/')[-1], sender=sender,
                          text='https://github.com/ROKT/launch-engineering/pull/6000 STAMP mani.vedma@rokt.com')
            self.store.ingest(self.config, [msg], START, END, None)
        self.assertEqual(self.store.history(), [])

    def test_ci_uses_latest_buildkite_context_and_waits_for_incomplete_checks(self):
        statuses = [{'id': 1, 'context': 'buildkite/pipeline', 'state': 'failure', 'created_at': '2026-10-26T18:00:00Z'},
                    {'id': 2, 'context': 'buildkite/pipeline', 'state': 'success', 'created_at': '2026-10-26T18:01:00Z'}]
        self.assertEqual(review.ci_summary(statuses, [])['state'], 'passed')
        self.assertEqual(review.ci_summary(statuses, [{'name': 'tests', 'status': 'in_progress', 'conclusion': None}])['state'], 'pending')
        self.assertEqual(review.ci_summary(statuses, [{'name': 'tests', 'status': 'completed', 'conclusion': 'cancelled'}])['state'], 'failed')
        self.assertEqual(review.ci_summary([], [])['state'], 'none')

    def test_stamp_head_change_requires_another_explicit_request(self):
        job = self.enqueue(message(text='https://github.com/ROKT/launch-engineering/pull/6000 STAMP',
            sender={'name': 'users/123456789', 'email': 'mani.vedma@rokt.com', 'type': 'HUMAN'}))
        self.github.move_after_post = True
        review.review_one(self.config, self.store, self.github, NoModel(), job)
        self.assertEqual(self.store.history()[0]['status'], 'deferred')
        self.assertIsNone(self.store.claim())
        self.assertEqual(len(self.github.submitted), 1)

    def test_interrupted_model_run_is_not_automatically_repeated_on_restart(self):
        job = self.enqueue()
        self.store.start_model_run(job['id'])
        self.store.recover()
        self.assertEqual(self.store.history()[0]['status'], 'needs-attention')
        self.assertIsNone(self.store.claim())

    def test_two_second_poll_and_configurable_reactions_keep_original_thread(self):
        self.assertEqual(self.config.poll_seconds, 2)
        custom = {**REACTIONS, 'approved': '🎉'}
        config = Config(SPACE, START, reactions=custom)
        self.store.reactions = custom
        self.store.ingest(config, [message()], START, END, None)
        job = self.store.claim()
        self.store.finish(job, 'submitted', 'Approved', 'approved')
        self.store.finish(job, 'submitted', 'Approved', 'approved')
        reactions = [json.loads(row['payload'])['emoji']['unicode'] for row in self.store.db.execute("select * from outbox where kind='reaction'")]
        self.assertEqual(reactions, ['👀', '🎉'])
        replies = self.store.db.execute("select * from outbox where kind='message'").fetchall()
        self.assertTrue(all(json.loads(row['payload'])['thread']['name'] == message()['thread']['name'] for row in replies))

    def test_warning_for_merged_pr(self):
        job = self.enqueue()
        self.github.data.update(merged=True, state='closed')
        review.review_one(self.config, self.store, self.github, NoModel(), job)
        reactions = [json.loads(row[0])['emoji']['unicode'] for row in self.store.db.execute("select payload from outbox where kind='reaction'")]
        self.assertEqual(reactions, ['👀', '⚠️'])
        self.assertIn('PR is merged', self.store.history()[0]['result'])

    def test_out_of_scope_pr_gets_warning_without_queue_or_model(self):
        self.store.ingest(self.config, [message(text='https://github.com/ROKT/catalog/pull/6191')], START, END, None)
        self.assertEqual(self.store.history(), [])
        reaction = self.store.db.execute("select payload from outbox where kind='reaction'").fetchone()
        self.assertEqual(json.loads(reaction[0])['emoji']['unicode'], '⚠️')

    def test_pr_merged_during_submission_reports_actual_review_with_warning(self):
        from test_review import Reviewer
        job = self.enqueue()
        submit = self.github.submit
        def closing(*args):
            result = submit(*args)
            self.github.data.update(state='closed', merged=True)
            return result
        self.github.submit = closing
        review.review_one(self.config, self.store, self.github, Reviewer(), job)
        self.assertEqual(len(self.github.submitted), 1)
        self.assertEqual(self.store.history()[0]['status'], 'submitted')
        self.assertIn('PR is now closed', self.store.history()[0]['result'])
        reactions = [json.loads(row[0])['emoji']['unicode'] for row in self.store.db.execute("select payload from outbox where kind='reaction'")]
        self.assertEqual(reactions, ['👀', '⚠️'])

    def test_head_change_cancels_running_model_and_records_unknown_usage(self):
        reviewer = review.Reviewer(Path(self.tmp.name))
        child = Mock(returncode=-15)
        child.poll.return_value = None
        guard = Mock(side_effect=[None, review.HeadMoved({'head': {'sha': 'c' * 40}})])
        with patch('review.subprocess.Popen', return_value=child), patch('review.time.monotonic', side_effect=[0, 0, 20, 20]):
            with self.assertRaises(review.HeadMoved):
                reviewer.invoke('fixed test prompt', guard=guard)
        child.terminate.assert_called_once()
        run = self.store.costs()['runs'][-1]
        self.assertEqual(run['status'], 'cancelled')
        self.assertIsNone(run['estimated_credits'])

    def test_outbox_limits_messages_without_starving_receipt_reactions(self):
        self.enqueue()
        self.store.ingest(self.config, [message('another', text='https://github.com/ROKT/launch-engineering/pull/6001')], START, END, None)
        sent = []
        class Chat:
            def send(inner, row):
                sent.append(row['kind'])
                return {'name': 'delivered'}
        flush_outbox(self.store, Chat())
        self.assertEqual(sent.count('message'), 1)
        self.assertEqual(sent.count('reaction'), 2)

    def test_folder_stamp_requires_an_actual_pr_link(self):
        self.store.ingest(self.config, [message(text='https://github.com/ROKT/launch-engineering/tree/main/Orbit STAMP',
            sender={'name': 'users/123456789', 'email': 'mani.vedma@rokt.com', 'type': 'HUMAN'})], START, END, None)
        self.assertEqual(self.store.history(), [])
        replies = [json.loads(row[0])['text'] for row in self.store.db.execute("select payload from outbox where kind='message'")]
        self.assertTrue(any('PR link' in text for text in replies))


if __name__ == '__main__':
    unittest.main()
