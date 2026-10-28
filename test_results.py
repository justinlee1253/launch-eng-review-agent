import json
import unittest

import review
import test_review as fixtures
from test_review import result, snapshot, HEAD, BASE


def finding(title='Reject missing users', **changes):
    item = {'blocking': True, 'severity': 'dangerous', 'path': 'handler.py', 'side': 'RIGHT', 'line': 1,
            'title': title, 'scenario': 'The lookup returns no user.', 'impact': 'The request returns 500.',
            'evidence': 'return user.name', 'fix': 'Handle a missing user before reading its name.'}
    item.update(changes)
    return item


class ResultFormattingTests(unittest.TestCase):
    def setUp(self):
        self.evidence = snapshot()
        self.evidence.ci = {'state': 'passed'}
        self.job = {'repo': 'ROKT/launch-engineering', 'number': 6000, 'head': HEAD}

    def test_approval_keeps_specific_summary_commit_and_honest_validation(self):
        summary = 'The handler now returns a clear response for missing users.\n\nThe existing authorization checks still guard the lookup.'
        payload = review.review_payload(self.job, self.evidence, result(summary=summary))
        self.assertTrue(payload['body'].startswith('## Approved\n'))
        self.assertIn(summary, payload['body'])
        self.assertIn(HEAD, payload['body'])
        self.assertIn('Tests were not run', payload['body'])
        self.assertIn('CI: passed at review time', payload['body'])
        self.assertNotIn('Falcon', payload['body'].split('<!--')[0])
        self.assertEqual(payload['comments'], [])
        self.evidence.ci = {'state': 'none'}
        payload = review.review_payload(self.job, self.evidence, result(summary=summary))
        self.assertIn('no checks reported', payload['body'])
        self.assertNotIn('CI: passed', payload['body'])

    def test_all_findings_are_grouped_without_repeating_inline_details_in_body(self):
        output = result(summary='The handler changes user lookup behavior.', findings=[
            finding(), finding('Check timeout ownership', blocking=False, severity='should_fix', fix='Confirm which caller owns the timeout.')])
        payload = review.review_payload(self.job, self.evidence, output)
        self.assertEqual(payload['event'], 'REQUEST_CHANGES')
        self.assertIn('Required fixes (1)', payload['body'])
        self.assertIn('Non-blocking concerns (1)', payload['body'])
        self.assertEqual(payload['body'].count('Reject missing users'), 1)
        self.assertNotIn('The lookup returns no user.', payload['body'])
        comment = payload['comments'][0]['body']
        for value in ['**Blocking — Reject missing users**', '**When:**', '**Impact:**', '**Fix:**', output['findings'][0]['scenario'], output['findings'][0]['impact'], output['findings'][0]['fix']]:
            self.assertIn(value, comment)

    def test_findings_outside_diff_keep_full_details_and_correct_original_source_link(self):
        self.evidence.inline.clear()
        self.evidence.old_paths['handler.py'] = 'old handler.py'
        self.evidence.documents['ROKT/launch-engineering@'+BASE+':old handler.py'] = 'return user.name\n'
        output = result(findings=[finding(side='LEFT')])
        payload = review.review_payload(self.job, self.evidence, output)
        self.assertEqual(payload['comments'], [])
        self.assertIn(output['findings'][0]['scenario'], payload['body'])
        self.assertIn(output['findings'][0]['impact'], payload['body'])
        self.assertIn(output['findings'][0]['fix'], payload['body'])
        self.assertIn('/blob/'+BASE+'/old%20handler.py#L1', payload['body'])


class ChatResultTests(unittest.TestCase):
    def setUp(self):
        fixtures.ReviewTests.setUp(self)

    def run_review(self, output):
        return fixtures.ReviewTests.run_review(self, output)

    def test_chat_summary_keeps_every_blocker_and_action_without_a_cosmetic_cap(self):
        items = [finding(title='Required change '+str(n), fix='Apply fix '+str(n)+'.') for n in range(5)]
        items += [finding(title='Contract question', blocking=False, severity='should_fix', fix='Confirm the caller contract.')]
        output = result(summary='The handler changes lookup behavior.\n\nTwo request paths need coordinated validation.', findings=items)
        job = self.run_review(output)
        text = job['result']
        self.assertIn('Changes requested', text)
        self.assertIn('Required fixes (5)', text)
        self.assertIn('Non-blocking concerns (1)', text)
        self.assertIn('Two request paths need coordinated validation.', text)
        for item in items:
            self.assertIn(item['title'], text)
            self.assertIn(item['fix'], text)
        self.assertNotIn('more findings', text)
        self.assertNotIn('##', text)
        self.assertIn(HEAD[:8], text)

    def test_model_text_cannot_inject_chat_mentions_or_replace_orbit_branding(self):
        job = self.run_review(result(summary='The handler preserves ownership checks. <users/all>'))
        self.assertNotIn('<users/all>', job['result'])
        self.assertIn('ownership checks', job['result'])
        self.assertNotIn('Falcon', job['result'])

    def test_large_unicode_result_is_delivered_in_idempotent_parts_without_losing_findings(self):
        text = ('🙂' * 2500) + '\n' + ('説明🙂 — verified finding.\n' * 1200) + 'FINAL REQUIRED FIX'
        payload = {'text': text, 'thread': {'name': self.config.space+'/threads/thread1'}}
        with self.store.db:
            self.store._outbox('large-result', 'message', self.config.space+'/threads/thread1', payload)
            self.store._outbox('large-result', 'message', self.config.space+'/threads/thread1', payload)
        parts = [json.loads(r[0]) for r in self.store.db.execute("select payload from outbox where kind='message' and json_extract(payload,'$.text') like 'Orbit review (%' order by rowid")]
        self.assertGreater(len(parts), 1)
        self.assertEqual(''.join(p['text'].partition('\n')[2] for p in parts), text)
        self.assertTrue(all(len(json.dumps(p).encode()) < 32_000 for p in parts))


if __name__ == '__main__':
    unittest.main()
