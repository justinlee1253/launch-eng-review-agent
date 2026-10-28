import json
from pathlib import Path
import tempfile
import unittest

import agent
from test_agent import SPACE, START, END, message


MAINTAINER = '<users/104386993234033491963>'


class MessageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = agent.Store(Path(self.tmp.name) / 'state.sqlite3')
        self.addCleanup(self.store.db.close)
        self.config = agent.Config(SPACE, START)
        self.store.ingest(self.config, [message()], START, END, None)
        self.job = self.store.claim()

    def finish(self, reason, status='needs-attention'):
        self.store.finish(self.job, status, f"{self.job['repo']}#{self.job['number']} · https://github.com/{self.job['repo']}/pull/{self.job['number']}\nReview held — " + reason)
        return self.store.history()[0]['result']

    def test_warnings_always_name_next_action_and_correct_owner(self):
        cases = [
            ('Referenced dependency changed during review: ROKT/launch-engineering#86', 'needs-attention', True, 'refresh'),
            ('CI/Buildkite failed, was cancelled, or needs action.', 'deferred', False, 'repost'),
            ('CI/Buildkite is still running.', 'waiting_ci', False, 'automatically'),
            ('Draft PR; post the link again when it is ready for review.', 'needs-attention', False, 'ready'),
            ('PR has merge conflicts. Resolve them and post the link again.', 'deferred', False, 'Resolve'),
            ('20-minute overall review budget exhausted.', 'needs-attention', True, 'retry'),
            ('Finding file, line, or code quotation was not verified.', 'needs-attention', True, 'citation'),
            ('Authenticated GitHub account differs from justinlee1253; no account was switched.', 'needs-attention', True, 'account'),
            ('Justin has a manual blocking review; do not overwrite that decision automatically.', 'needs-attention', True, 'manual review'),
            ('Private review draft storage is unavailable; no review may be published.', 'needs-attention', True, 'storage'),
            ('GitHubError; required GitHub evidence is unavailable.', 'needs-attention', True, 'GitHub'),
            ('Codex did not return a complete structured review; check saved login and usage.', 'needs-attention', True, 'retry'),
            ('Unexpected internal validation failure.', 'needs-attention', True, 'inspect'),
        ]
        for reason, status, operator, action in cases:
            with self.subTest(reason=reason):
                text = self.finish(reason, status)
                self.assertIn('\nNext: ', text)
                self.assertIn(action, text)
                self.assertEqual(MAINTAINER in text, operator)
                self.assertNotIn('Falcon', text)
                self.assertNotIn('Codex', text)
                self.assertNotIn('GitHubError', text)
        self.assertIn('#86', self.finish(cases[0][0]))
        ci = self.finish('CI/Buildkite failed, was cancelled, or needs action. buildkite/launch-engineering: FAILURE — https://buildkite.com/rokt/launch-engineering/builds/123 Fix it and post the link again.', 'deferred')
        self.assertIn('buildkite/launch-engineering: FAILURE', ci)
        self.assertIn('<https://buildkite.com/rokt/launch-engineering/builds/123|View CI checks>', ci)

    def test_uncertain_submission_never_claims_no_review_was_posted(self):
        self.store.update(self.job, payload=json.dumps({'event': 'APPROVE'}))
        text = self.finish('Ambiguous GitHub write; timeline cannot prove exactly one matching review.', 'ambiguous')
        self.assertIn('unconfirmed', text)
        self.assertIn('verify', text)
        self.assertIn(MAINTAINER, text)
        self.assertNotIn('No review posted', text)
        self.assertNotIn('No GitHub review was posted', text)

    def test_size_holds_do_not_claim_an_existing_manual_review(self):
        reasons = [
            'Evidence exceeds the pilot context budget; manual review is required.',
            'Saved review progress exceeds the context budget; manual review is required.',
            'Review input has 1,122,348 characters; the Codex CLI limit is 1,048,576. '
            'Compact the evidence or review manually; no model call started.',
        ]
        for reason in reasons:
            with self.subTest(reason=reason):
                text = self.finish(reason)
                self.assertIn('exceeds the supported text-review limits', text)
                self.assertNotIn('existing manual review', text)
                self.assertNotIn('resolve the manual review decision', text)

    def test_manual_review_warning_requires_a_known_review_condition(self):
        for reason in (
            'Justin has a manual blocking review; do not overwrite that decision automatically.',
            'Justin has an unfinished manual review at this head; ownership of the decision needs attention.',
            'Ledger records this head, but its effective review is missing or dismissed; manual decision required.',
            'An intervening Justin review/comment requires a manual decision.',
        ):
            with self.subTest(reason=reason):
                self.assertIn('existing manual review', self.finish(reason))
        self.assertNotIn('existing manual review', self.finish('Unexpected failure; inspect manually.'))

    def test_repeated_identical_warning_does_not_send_another_mention(self):
        reason = 'Referenced dependency changed during review: ROKT/launch-engineering#86'
        self.finish(reason)
        self.finish(reason)
        texts = [json.loads(r[0])['text'] for r in self.store.db.execute("select payload from outbox where kind='message'")]
        self.assertEqual(sum(MAINTAINER in text for text in texts), 1)

    def test_repost_links_to_original_request_without_another_job(self):
        self.store.ingest(self.config, [message('duplicate', createTime='2026-10-26T18:53:00Z')], START, END, None)
        text = json.loads(self.store.db.execute("select payload from outbox where kind='message' order by rowid desc limit 1").fetchone()[0])['text']
        self.assertIn('Original request', text)
        self.assertIn('https://chat.google.com/room/AAQAixK3auw/thread1', text)
        self.assertNotIn('2026-10-26T18:52', text)
        self.assertEqual(len(self.store.history()), 1)

    def test_posted_review_details_remain_available_for_stale_result_messages(self):
        self.store.record_review(self.job, 'justinlee1253', {'id': 321, 'html_url': 'https://github.com/ROKT/launch-engineering/pull/6000#pullrequestreview-321', 'state': 'APPROVED'})
        self.assertEqual(self.job.get('review_id'), 321)
        self.assertEqual(self.job.get('review_url'), 'https://github.com/ROKT/launch-engineering/pull/6000#pullrequestreview-321')

    def test_chat_requests_explicit_markup_for_host_mentions(self):
        calls = []
        chat = agent.Chat(self.config, manager=object())
        chat.request = lambda *args, **kwargs: calls.append(kwargs) or {}
        row = {'kind': 'message', 'target': SPACE+'/threads/thread1', 'id': 'fixed-request',
               'payload': json.dumps({'text': MAINTAINER+', inspect this run.', 'thread': {'name': SPACE+'/threads/thread1'}})}
        chat.send(row)
        self.assertEqual(calls[0]['json'].get('markupSyntax'), 'MARKUP_SYNTAX_CHAT')
        self.assertEqual(calls[0]['params']['requestId'], 'fixed-request')


if __name__ == '__main__':
    unittest.main()
