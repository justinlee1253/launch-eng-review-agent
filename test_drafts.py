import copy
import json
from pathlib import Path
import tempfile
import unittest

from agent import Config, Store
from test_agent import SPACE, START, END, message
from test_review import GitHub, HEAD, NEXT, result, snapshot
import review


def finding(**changes):
    row = {'blocking': True, 'severity': 'should_fix', 'path': 'handler.py', 'side': 'RIGHT',
           'line': 99, 'title': 'Missing user crashes the handler', 'scenario': 'A user is absent.',
           'impact': 'The request returns 500.', 'evidence': 'return user.name',
           'fix': 'Handle the missing user before reading its name.'}
    row.update(changes)
    return row


class Reviewer:
    def __init__(self, output, correction=None):
        self.output, self.correction = output, correction
        self.reviews, self.repairs = 0, 0

    def review(self, github, evidence):
        self.reviews += 1
        return copy.deepcopy(self.output)

    def invoke(self, prompt, **options):
        self.repairs += 1
        if options.get('guard'):
            options['guard']()
        return copy.deepcopy(self.correction or {'corrections': [], 'unresolved': [{'finding_index': 0, 'reason': 'Cannot verify.'}]})


class DraftRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'state.sqlite3')
        self.addCleanup(self.store.db.close)
        self.config = Config(SPACE, START)
        self.github = GitHub()
        self.store.ingest(self.config, [message()], START, END, None)
        self.job = self.store.claim()

    def run_review(self, reviewer):
        review.review_one(self.config, self.store, self.github, reviewer, self.job)
        return self.store.db.execute('select * from jobs where id=?', (self.job['id'],)).fetchone()

    def draft(self):
        files = list((self.root / 'drafts').glob('*.json'))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(files[0].parent.stat().st_mode & 0o777, 0o700)
        return json.loads(files[0].read_text())

    def test_unique_exact_quote_repairs_line_without_another_model_call(self):
        reviewer = Reviewer(result(findings=[finding()]))
        self.assertEqual(self.run_review(reviewer)['status'], 'submitted')
        self.assertEqual(reviewer.reviews, 1)
        self.assertEqual(reviewer.repairs, 0)
        self.assertEqual(self.github.submitted[0]['event'], 'REQUEST_CHANGES')
        self.assertEqual(self.github.submitted[0]['comments'][0]['line'], 1)
        draft = self.draft()
        self.assertEqual(draft['original']['findings'][0]['line'], 99)
        self.assertEqual(draft['current']['findings'][0]['line'], 1)

    def test_bad_quote_gets_one_citation_only_pass_and_keeps_blocker(self):
        correction = {'corrections': [{'finding_index': 0, 'path': 'handler.py', 'side': 'RIGHT',
                      'line': 1, 'evidence': 'return user.name'}], 'unresolved': []}
        reviewer = Reviewer(result(findings=[finding(line=1, evidence='return user.display_name')]), correction)
        self.assertEqual(self.run_review(reviewer)['status'], 'submitted')
        self.assertEqual((reviewer.reviews, reviewer.repairs), (1, 1))
        self.assertEqual(self.github.submitted[0]['event'], 'REQUEST_CHANGES')
        self.assertEqual(self.draft()['original']['findings'][0]['evidence'], 'return user.display_name')

    def test_unresolved_citation_retains_exact_reason_and_does_not_repeat_analysis_on_retry(self):
        reviewer = Reviewer(result(findings=[finding(line=1, evidence='invented quote')]))
        held = self.run_review(reviewer)
        self.assertEqual(held['status'], 'needs-attention')
        self.assertIn('handler.py', held['result'])
        self.assertIn('Finding 1', held['result'])
        self.assertEqual(self.github.submitted, [])
        self.assertEqual(self.draft()['issues'][0]['reason'], 'quote_mismatch')
        self.store.db.close()
        self.store = Store(self.root / 'state.sqlite3')
        self.addCleanup(self.store.db.close)
        self.store.ingest(self.config, [message('repost')], START, END, None)
        self.job = self.store.claim()
        self.github.capture = lambda *args: self.fail('A saved draft must not repeat evidence capture.')
        self.assertEqual(self.run_review(reviewer)['status'], 'needs-attention')
        self.assertEqual((reviewer.reviews, reviewer.repairs), (1, 1))

    def test_repair_cannot_change_severity_or_drop_a_blocking_finding(self):
        correction = {'corrections': [{'finding_index': 0, 'path': 'handler.py', 'side': 'RIGHT',
                       'line': 1, 'evidence': 'return user.name', 'blocking': False}], 'unresolved': []}
        reviewer = Reviewer(result(findings=[finding(line=1, evidence='wrong')]), correction)
        self.assertEqual(self.run_review(reviewer)['status'], 'needs-attention')
        self.assertEqual(reviewer.repairs, 1)
        self.assertEqual(self.github.submitted, [])
        self.assertTrue(self.draft()['current']['findings'][0]['blocking'])

    def test_ambiguous_exact_quote_is_not_silently_relocated(self):
        evidence = snapshot()
        evidence.documents['ROKT/launch-engineering@' + HEAD + ':handler.py'] *= 2
        self.github.capture = lambda *args: evidence
        reviewer = Reviewer(result(findings=[finding()]))
        self.assertEqual(self.run_review(reviewer)['status'], 'needs-attention')
        self.assertEqual(self.draft()['current']['findings'][0]['line'], 99)
        self.assertEqual(self.github.submitted, [])

    def test_head_move_during_correction_prevents_submission(self):
        reviewer = Reviewer(result(findings=[finding(line=1, evidence='wrong')]))
        def invoke(*args, **options):
            self.assertEqual(options['timeout'], 120)
            self.github.data['head']['sha'] = NEXT
            return {'corrections': [{'finding_index': 0, 'path': 'handler.py', 'side': 'RIGHT',
                     'line': 1, 'evidence': 'return user.name'}], 'unresolved': []}
        reviewer.invoke = invoke
        self.assertEqual(self.run_review(reviewer)['status'], 'queued')
        self.assertEqual(self.github.submitted, [])
        self.assertEqual(self.draft()['repair_calls'], 1)

    def test_corrupt_saved_draft_cannot_silently_start_another_review(self):
        reviewer = Reviewer(result(findings=[finding(line=1, evidence='wrong')]))
        self.run_review(reviewer)
        path = next((self.root / 'drafts').glob('*.json'))
        path.write_text('{corrupt')
        self.store.update(self.job, status='queued')
        self.job = self.store.claim()
        self.assertEqual(self.run_review(reviewer)['status'], 'needs-attention')
        self.assertEqual(reviewer.reviews, 1)
        self.assertEqual(self.github.submitted, [])
