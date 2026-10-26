import copy
import json
from pathlib import Path
import tempfile
import unittest

from agent import Config, Store, flush_outbox
from test_agent import SPACE, START, END, message
import review


HEAD = 'a' * 40
BASE = 'b' * 40
NEXT = 'c' * 40
PR = {'state': 'open', 'draft': False, 'merged': False, 'user': {'login': 'coworker'},
      'head': {'sha': HEAD}, 'base': {'sha': BASE}, 'changed_files': 1,
      'title': 'Fix handler', 'body': 'Behavior description'}


def result(**changes):
    value = {'status': 'complete', 'summary': 'No material blockers found.', 'reviewed_head': HEAD,
             'reviewed_files': ['handler.py'], 'lenses': ['correctness', 'security', 'operability'],
             'requests': [], 'findings': []}
    value.update(changes)
    return value


def snapshot():
    value = review.Evidence('ROKT/launch-engineering', HEAD, BASE, BASE)
    value.files = ['handler.py']
    value.documents['ROKT/launch-engineering@' + HEAD + ':handler.py'] = 'return user.name\n'
    value.diff = '--- a/handler.py\n+++ b/handler.py\n@@ -1 +1 @@\n-return user\n+return user.name\n'
    value.inline = {('handler.py', 'RIGHT', 1), ('handler.py', 'LEFT', 1)}
    return value


class GitHub:
    def __init__(self):
        self.data = copy.deepcopy(PR)
        self.timeline = []
        self.submitted = []
        self.login = 'justinlee1253'
        self.move_after_post = False
        self.lose_response = False
        self.fail_timeline = False

    def actor(self):
        return self.login

    def pr(self, repo, number):
        return copy.deepcopy(self.data)

    def reviews(self, repo, number):
        if self.fail_timeline:
            raise review.GitHubError('timeline unavailable')
        return copy.deepcopy(self.timeline)

    def comments(self, repo, number):
        return []

    def ci(self, repo, head):
        return {'state': 'passed', 'checks': ['buildkite/pipeline']}

    def capture(self, job, pr):
        return snapshot()

    def submit(self, job, payload):
        self.submitted.append(payload)
        response = {'id': 123, 'html_url': 'https://github.com/ROKT/launch-engineering/pull/6000#pullrequestreview-123',
                    'state': 'APPROVED' if payload['event'] == 'APPROVE' else 'CHANGES_REQUESTED',
                    'user': {'login': self.login}, 'commit_id': payload['commit_id'], 'body': payload['body']}
        self.timeline.append(response)
        if self.move_after_post:
            self.data['head']['sha'] = NEXT
        if self.lose_response:
            raise review.AmbiguousWrite('lost response')
        return response


class Reviewer:
    def __init__(self, output=None):
        self.output = output if output is not None else result()
    def review(self, github, evidence):
        return self.output


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'state.sqlite3')
        self.addCleanup(self.store.db.close)
        self.config = Config(SPACE, START)
        self.github = GitHub()
        self.store.ingest(self.config, [message()], START, END, None)
        self.job = self.store.claim()

    def run_review(self, output=None):
        review.review_one(self.config, self.store, self.github, Reviewer(output), self.job)
        return self.store.history()[0]

    def test_complete_clean_review_posts_one_commit_anchored_approval(self):
        job = self.run_review()
        self.assertEqual(job['status'], 'submitted')
        self.assertEqual(len(self.github.submitted), 1)
        payload = self.github.submitted[0]
        self.assertEqual(payload['event'], 'APPROVE')
        self.assertEqual(payload['commit_id'], HEAD)
        self.assertIn('<!-- justin-falcon-review:ROKT/launch-engineering#6000:' + HEAD + ' -->', payload['body'])
        self.assertIn('Tests were not run', payload['body'])
        self.assertEqual(self.store.reviewed('ROKT/launch-engineering', 6000, HEAD, 'justinlee1253')['review_id'], 123)

    def test_substantiated_blocker_is_bundled_as_inline_request_changes(self):
        finding = {'blocking': True, 'severity': 'dangerous', 'path': 'handler.py', 'side': 'RIGHT', 'line': 1,
                   'title': 'Null user crashes handler', 'scenario': 'A lookup returns no user.',
                   'impact': 'All such requests return 500.', 'evidence': 'return user.name',
                   'fix': 'Handle the missing user before accessing name.'}
        job = self.run_review(result(findings=[finding]))
        payload = self.github.submitted[0]
        self.assertEqual(job['status'], 'submitted')
        self.assertEqual(payload['event'], 'REQUEST_CHANGES')
        self.assertEqual(payload['comments'][0]['line'], 1)
        self.assertEqual(payload['comments'][0]['side'], 'RIGHT')
        self.assertIn('**Blocking:**', payload['comments'][0]['body'])
        reactions = [json.loads(row[0])['emoji']['unicode'] for row in self.store.db.execute("select payload from outbox where kind='reaction'")]
        self.assertEqual(reactions, ['👀', '💬'])

    def test_incomplete_missing_lens_and_missing_file_cannot_approve(self):
        for output in [result(status='incomplete'), result(lenses=['security']), result(reviewed_files=[])]:
            with self.subTest(output=output):
                self.run_review(output)
        self.assertEqual(self.github.submitted, [])
        self.assertEqual(self.store.history()[0]['status'], 'needs-attention')

    def test_wrong_identity_cannot_submit(self):
        self.github.login = 'justin-lee_rokt'
        self.assertEqual(self.run_review()['status'], 'needs-attention')
        self.assertEqual(self.github.submitted, [])

    def test_manual_block_on_previous_head_is_not_overwritten(self):
        self.github.timeline = [{'id': 99, 'user': {'login': 'justinlee1253'}, 'state': 'CHANGES_REQUESTED',
                                 'commit_id': BASE, 'body': 'Manual review'}]
        self.assertEqual(self.run_review()['status'], 'needs-attention')
        self.assertEqual(self.github.submitted, [])

    def test_same_head_review_is_reused_after_repost(self):
        self.run_review()
        self.store.ingest(self.config, [message('repost')], START, END, None)
        self.job = self.store.claim()
        self.assertEqual(self.run_review()['status'], 'skipped')
        self.assertEqual(len(self.github.submitted), 1)

    def test_unfinished_manual_review_at_this_head_is_not_overwritten(self):
        for state in ('COMMENTED', 'PENDING'):
            with self.subTest(state=state):
                self.github.timeline = [{'id': 99, 'user': {'login': 'justinlee1253'}, 'state': state,
                                        'commit_id': HEAD, 'body': 'Manual review in progress'}]
                self.assertEqual(self.run_review()['status'], 'needs-attention')
        self.assertEqual(self.github.submitted, [])

    def test_other_justin_accounts_active_manual_block_remains_effective(self):
        self.github.timeline = [
            {'id': 98, 'user': {'login': 'justin-lee_rokt'}, 'state': 'CHANGES_REQUESTED', 'commit_id': BASE, 'body': 'Manual block'},
            {'id': 99, 'user': {'login': 'justinlee1253'}, 'state': 'APPROVED', 'commit_id': BASE, 'body': '<!-- justin-falcon-review:old -->'},
        ]
        self.assertEqual(self.run_review()['status'], 'needs-attention')
        self.assertEqual(self.github.submitted, [])

    def test_head_moves_before_post_so_old_verdict_is_not_sent(self):
        github = self.github
        class MovingReviewer(Reviewer):
            def review(inner, *args):
                github.data['head']['sha'] = NEXT
                return result()
        review.review_one(self.config, self.store, github, MovingReviewer(), self.job)
        self.assertEqual(self.store.history()[0]['status'], 'queued')
        self.assertEqual(github.submitted, [])

    def test_head_moves_during_capture_requeues_instead_of_using_partial_evidence(self):
        def capture(*args):
            self.github.data['head']['sha'] = NEXT
            raise review.HeadMoved(copy.deepcopy(self.github.data))
        self.github.capture = capture
        self.run_review()
        self.assertEqual(self.store.history()[0]['status'], 'queued')
        self.assertEqual(self.github.submitted, [])

    def test_head_moves_during_post_marks_prior_result_stale_and_requeues(self):
        self.github.move_after_post = True
        job = self.run_review()
        self.assertEqual(job['status'], 'queued')
        self.assertEqual(len(self.github.submitted), 1)
        self.assertIsNotNone(self.store.reviewed('ROKT/launch-engineering', 6000, HEAD, 'justinlee1253'))
        self.assertTrue(self.store.db.execute("select 1 from events where code='review_stale'").fetchone())

    def test_lost_successful_post_response_is_reconciled_without_retry(self):
        self.github.lose_response = True
        self.assertEqual(self.run_review()['status'], 'submitted')
        self.assertEqual(len(self.github.submitted), 1)

    def test_reserved_write_after_restart_is_never_blindly_retried(self):
        self.store.update(self.job, status='submitting', head=HEAD, marker='marker', payload=json.dumps({'event': 'APPROVE'}))
        self.store.recover()
        self.job = self.store.claim()
        self.assertEqual(self.run_review()['status'], 'needs-attention')
        self.assertEqual(self.github.submitted, [])

    def test_chat_failure_after_submission_does_not_rerun_review(self):
        self.run_review()
        class BrokenChat:
            def send(inner, row):
                from agent import TemporaryFailure
                raise TemporaryFailure('network')
        flush_outbox(self.store, BrokenChat())
        self.assertEqual(self.store.history()[0]['status'], 'submitted')
        self.assertIsNone(self.store.claim())

    def test_fabricated_file_line_or_quote_is_rejected(self):
        finding = {'blocking': True, 'severity': 'dangerous', 'path': 'fake.py', 'side': 'RIGHT', 'line': 500,
                   'title': 'Claim', 'scenario': 'trigger', 'impact': 'bad', 'evidence': 'invented', 'fix': 'fix'}
        self.assertEqual(self.run_review(result(findings=[finding]))['status'], 'needs-attention')
        self.assertEqual(self.github.submitted, [])

    def test_prior_heartbeat_review_is_imported(self):
        self.store.import_previous()
        self.assertEqual(self.store.reviewed('ROKT/catalog', 5962,
            'bd124867ba848a9df8e0f1013900b82b17453170', 'justinlee1253')['review_id'], 5618599675)


if __name__ == '__main__':
    unittest.main()
