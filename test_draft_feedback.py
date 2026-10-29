"""Draft feedback must stay comment-only and never consume a formal review."""
import json
from pathlib import Path
import tempfile
import unittest

from agent import Config, ConfigError, Store
from test_agent import SPACE, START, END, message
from test_review import GitHub, HEAD, NEXT, Reviewer, result
from test_drafts import finding
import review


class NoModel:
    def review(self, *args):
        raise AssertionError('A reused result must not spend model tokens')


class DraftFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'state.sqlite3')
        self.addCleanup(self.store.db.close)
        self.config, self.github = Config(SPACE, START), GitHub()
        self.github.data['draft'] = True
        self.store.ingest(self.config, [message()], START, END, None)
        self.job = self.store.claim()

    def run_review(self, reviewer=None):
        review.review_one(self.config, self.store, self.github, reviewer or Reviewer(), self.job)
        return next(row for row in self.store.history() if row['id'] == self.job['id'])

    def repost(self, name='again'):
        self.store.ingest(self.config, [message(name)], START, END, None)
        self.job = self.store.claim()
        self.assertIsNotNone(self.job)

    def test_clean_draft_posts_comment_only_feedback_in_github_and_chat(self):
        reviewer = review.Reviewer(self.root)
        reviewer.invoke = lambda *args, **kwargs: result()
        job = self.run_review(reviewer)
        self.assertEqual(job['status'], 'feedback')
        self.assertTrue(self.github.data['draft'])
        self.assertEqual([p['event'] for p in self.github.submitted], ['COMMENT'])
        self.assertEqual(self.github.submitted[0]['commit_id'], HEAD)
        self.assertIn('Draft feedback', self.github.submitted[0]['body'])
        self.assertIn('Draft feedback', job['result'])
        self.assertIn('#pullrequestreview-123', job['result'])
        self.assertNotIn('— Approved', job['result'])
        self.assertEqual(self.store.db.execute('select count(*) from ledger').fetchone()[0], 0)

    def test_blockers_are_inline_comments_with_one_neutral_feedback_status(self):
        job = self.run_review(Reviewer(result(findings=[finding()])))
        self.assertEqual(job['status'], 'feedback')
        payload = self.github.submitted[0]
        self.assertEqual(payload['event'], 'COMMENT')
        self.assertEqual(payload['comments'][0]['line'], 1)
        self.assertIn('Next:', job['result'])
        self.assertNotIn('— Changes requested', job['result'])
        reactions = [json.loads(row[0])['emoji']['unicode'] for row in self.store.db.execute("select payload from outbox where kind='reaction'")]
        self.assertEqual(reactions, ['💬'])
        self.assertEqual(job['outcome'], 'feedback')

    def test_same_draft_commit_reuses_feedback_without_another_model_or_write(self):
        self.run_review()
        self.repost()
        self.run_review(NoModel())
        self.assertEqual(len(self.github.submitted), 1)
        self.assertEqual(self.store.db.execute('select count(*) from draft_feedback').fetchone()[0], 1)

    def test_ready_repost_gets_a_fresh_formal_review_at_the_same_commit(self):
        self.run_review()
        self.github.data['draft'] = False
        self.repost()
        job = self.run_review(Reviewer(result(findings=[finding()])))
        self.assertEqual([p['event'] for p in self.github.submitted], ['COMMENT', 'REQUEST_CHANGES'])
        self.assertEqual(job['status'], 'submitted')
        self.assertEqual(self.store.db.execute('select count(*) from ledger').fetchone()[0], 1)

    def test_promotion_during_feedback_never_upgrades_to_approval(self):
        outer = self
        class Promote:
            def review(self, *args):
                outer.github.data['draft'] = False
                return result()
        job = self.run_review(Promote())
        self.assertEqual(job['status'], 'feedback')
        self.assertEqual([p['event'] for p in self.github.submitted], ['COMMENT'])
        self.assertFalse(self.github.data['draft'])

    def test_ready_request_while_feedback_is_active_is_not_swallowed(self):
        outer = self
        class PromoteAndRepost:
            def review(self, *args):
                outer.github.data['draft'] = False
                outer.store.ingest(outer.config, [message('ready-request')], START, END, None)
                return result()
        self.run_review(PromoteAndRepost())
        self.job = self.store.claim()
        self.assertIsNotNone(self.job)
        self.run_review()
        self.assertEqual([p['event'] for p in self.github.submitted], ['COMMENT', 'APPROVE'])

    def test_formal_review_becoming_draft_cannot_publish_approval(self):
        self.github.data['draft'] = False
        outer = self
        class MakeDraft:
            def review(self, *args):
                outer.github.data['draft'] = True
                return result()
        self.run_review(MakeDraft())
        self.assertEqual(self.github.submitted, [])

    def test_ambiguous_feedback_is_reconciled_after_restart_without_resubmission(self):
        self.github.lose_response = True
        submit = self.github.submit
        def ambiguous(job, payload):
            try:
                return submit(job, payload)
            finally:
                self.github.fail_timeline = True
        self.github.submit = ambiguous
        self.assertEqual(self.run_review()['status'], 'ambiguous')
        reopened = Store(self.root / 'state.sqlite3')
        self.addCleanup(reopened.db.close)
        self.store = reopened
        self.store.recover()
        self.github.fail_timeline = False
        with self.store.db:
            self.store.db.execute('update jobs set ready_after=0')
        self.job = self.store.claim()
        self.assertEqual(self.run_review(NoModel())['status'], 'feedback')
        self.assertEqual(len(self.github.submitted), 1)
        self.assertEqual(self.store.db.execute('select count(*) from ledger').fetchone()[0], 0)

    def test_stale_head_never_publishes_feedback(self):
        outer = self
        class Move:
            def review(self, *args):
                outer.github.data['head']['sha'] = NEXT
                return result()
        self.run_review(Move())
        self.assertEqual(self.github.submitted, [])

    def test_incomplete_draft_review_never_publishes_a_comment(self):
        self.run_review(Reviewer(result(status='incomplete')))
        self.assertEqual(self.github.submitted, [])

    def test_draft_feedback_still_waits_for_gating_ci(self):
        self.github.ci = lambda *args: {'state': 'pending', 'checks': ['buildkite/test']}
        self.assertEqual(self.run_review(NoModel())['status'], 'waiting_ci')
        self.assertEqual(self.github.submitted, [])

    def test_cancellation_before_submission_never_publishes_feedback(self):
        outer = self
        class Cancel:
            def review(self, *args):
                with outer.store.db:
                    outer.store.db.execute('insert into cancellations values(?,?)', (outer.job['id'], END))
                return result()
        self.assertEqual(self.run_review(Cancel())['status'], 'cancelled')
        self.assertEqual(self.github.submitted, [])

    def test_draft_owned_by_connected_account_gets_comments_only(self):
        self.github.data['user']['login'] = 'justinlee1253'
        self.assertEqual(self.run_review()['status'], 'feedback')
        self.assertEqual([p['event'] for p in self.github.submitted], ['COMMENT'])

    def test_closing_during_comment_submission_leaves_a_warning(self):
        submit = self.github.submit
        def close(job, payload):
            row = submit(job, payload)
            self.github.data['state'] = 'closed'
            return row
        self.github.submit = close
        job = self.run_review()
        self.assertEqual([p['event'] for p in self.github.submitted], ['COMMENT'])
        self.assertEqual(job['outcome'], 'warning')
        self.assertIn('closed', job['result'])

    def test_stamp_never_reviews_or_approves_a_draft(self):
        self.store.finish(self.job, 'cancelled', 'Fixture setup')
        (self.root / 'stamp-permissions.json').write_text(json.dumps({'allowed_emails': ['mani.vedma@rokt.com']}))
        msg = message('stamp', text='https://github.com/ROKT/launch-engineering/pull/6000 STAMP',
                      sender={'name': 'users/123456789', 'email': 'mani.vedma@rokt.com', 'type': 'HUMAN'})
        self.store.ingest(self.config, [msg], START, END, None)
        self.job = self.store.claim()
        self.run_review(NoModel())
        self.assertEqual(self.github.submitted, [])
        self.assertTrue(self.github.data['draft'])

    def test_draft_mode_rejects_verdicts_before_any_github_write(self):
        self.store.update(self.job, mode='draft_feedback')
        github = review.GitHub(self.config, self.root)
        for event in ('APPROVE', 'REQUEST_CHANGES'):
            with self.subTest(event=event):
                with self.assertRaises(ConfigError):
                    self.store.reserve_submission(self.job, {'event': event}, 'fixture-marker')
                with self.assertRaises(review.Incomplete):
                    github.submit(self.job, {'event': event})
        self.assertEqual(list(self.root.glob('payloads/*')), [])

    def test_saved_feedback_is_reused_after_a_transient_pre_submit_failure(self):
        outer = self
        class LoseConnection:
            def review(self, *args):
                outer.github.fail_timeline = True
                return result()
        self.assertEqual(self.run_review(LoseConnection())['status'], 'needs-attention')
        self.github.fail_timeline = False
        self.repost()
        self.github.capture = lambda *args: self.fail('Must reuse captured draft evidence')
        self.assertEqual(self.run_review(NoModel())['status'], 'feedback')
        self.assertEqual([p['event'] for p in self.github.submitted], ['COMMENT'])

    def test_formal_submission_cannot_reserve_an_unknown_event(self):
        with self.assertRaises(ConfigError):
            self.store.reserve_submission(self.job, {'event': 'UNKNOWN'}, 'fixture-marker')
        self.assertEqual(self.store.history()[0]['status'], 'reviewing')


if __name__ == '__main__':
    unittest.main()
