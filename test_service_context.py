"""Service references and docs reach the reviewer only for the service a PR changes."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import review
import service_context
from test_context import Remote
from test_review import BASE, HEAD


TELEMETRY = 'prism-qa/QAProxyMVP/QAProxyMVP/Services/TelemetryService.swift'


class ServiceContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.references = self.root / 'references'
        self.references.mkdir()
        (self.references / 'prism-qa.md').write_text('prism-reference-marker: telemetry carries no notes.')
        patcher = patch.object(service_context, 'REFERENCES', self.references)
        patcher.start()
        self.addCleanup(patcher.stop)

    def capture(self, docs=None, **options):
        remote = Remote(self.root, **{'path': TELEMETRY, **options})
        remote.source.update(docs if docs is not None else {
            (HEAD, 'prism-qa/.claude/prism_overview.md'): 'overview-marker',
            (HEAD, 'prism-qa/.claude/prism_telemetry.md'): 'telemetry-doc-marker',
            (HEAD, 'prism-qa/.claude/prism_export.md'): 'export-doc-marker'})
        evidence = remote.capture({'repo': 'ROKT/launch-engineering', 'number': 6000}, remote.data)
        evidence.number, evidence.job_id = 6000, 'fixture-job'
        return remote, evidence

    def prompt(self, remote, evidence):
        reviewer = review.Reviewer(self.root)
        prompts = []
        def invoke(prompt, **kwargs):
            prompts.append(prompt)
            return {'status': 'incomplete', 'summary': 'Fixture only'}
        reviewer.invoke = invoke
        reviewer.review(remote, evidence)
        return prompts[-1]

    def test_prism_change_puts_reference_and_matching_docs_in_prompt(self):
        remote, evidence = self.capture()
        prompt = self.prompt(remote, evidence)
        self.assertIn('prism-reference-marker', prompt)
        self.assertIn('overview-marker', prompt)
        self.assertIn('telemetry-doc-marker', prompt)
        self.assertNotIn('export-doc-marker', prompt)
        profile = evidence.as_dict()['service_profiles']['prism-qa']
        self.assertEqual(profile['affected_paths'], [TELEMETRY])
        self.assertEqual(profile['deferred_docs'], [])

    def test_unrelated_service_receives_no_service_reference(self):
        remote, evidence = self.capture(path='sibling/handler.py')
        self.assertEqual(evidence.services, {})
        self.assertNotIn('prism-reference-marker', self.prompt(remote, evidence))

    def test_rename_out_and_deletion_still_select_the_service(self):
        for options in ({'path': 'sibling/handler.py', 'old': TELEMETRY, 'status': 'renamed'},
                        {'path': TELEMETRY, 'status': 'removed'}):
            with self.subTest(options=options):
                _, evidence = self.capture(**options)
                self.assertIn('prism-qa', evidence.services)

    def test_shared_pipeline_requires_changed_line_naming_the_service(self):
        for before, after, selected in [
            ('command: cd prism-qa/ && make\n', 'command: cd sibling/ && make\n', True),
            ('# prism-qa/ is unrelated here\ncommand: old\n', '# prism-qa/ is unrelated here\ncommand: new\n', False),
        ]:
            with self.subTest(selected=selected):
                _, evidence = self.capture(path='.buildkite/pipeline.yaml', before=before, after=after)
                self.assertEqual('prism-qa' in evidence.services, selected)

    def test_topic_doc_past_headroom_is_deferred_not_failed(self):
        big = 'x' * (review.MAX_CONTEXT - service_context.HEADROOM)
        remote, evidence = self.capture(docs={(HEAD, 'prism-qa/.claude/prism_telemetry.md'): big})
        profile = evidence.services['prism-qa']
        self.assertEqual(profile['deferred_docs'], ['prism-qa/.claude/prism_telemetry.md'])
        self.assertNotIn('ROKT/launch-engineering@' + HEAD + ':prism-qa/.claude/prism_telemetry.md', evidence.documents)

    def test_baseline_doc_falls_back_to_merge_base(self):
        _, evidence = self.capture(docs={(BASE, 'prism-qa/.claude/prism_overview.md'): 'base-overview'})
        self.assertEqual(evidence.documents['ROKT/launch-engineering@' + BASE + ':prism-qa/.claude/prism_overview.md'],
                         'base-overview')

    def test_missing_reference_holds_the_review(self):
        (self.references / 'prism-qa.md').unlink()
        with self.assertRaises(review.Incomplete):
            self.capture()

    def test_reference_and_profile_reach_the_audit_and_survive_a_draft(self):
        remote, evidence = self.capture()
        self.prompt(remote, evidence)
        audit = json.loads(next((self.root / 'context').glob('*.json')).read_text())
        self.assertIn('prism-qa', audit['profiles'])
        self.assertIn('prism-qa', audit['services'])
        self.assertTrue(any(row['path'].endswith('prism-qa.md') for row in audit['sources']))
        self.assertNotIn('prism-reference-marker', json.dumps(audit))

    def test_topics_follow_the_changed_area(self):
        cases = {
            'prism-qa/.buildkite/steps/verify-artifact.sh': 'prism-qa/RELEASING.md',
            'prism-qa/distribution/homebrew/prism-qa.rb': 'prism-qa/RELEASING.md',
            'prism-qa/QAProxyMVP/QAProxyMVP/Resources/har_capture.py': 'prism-qa/.claude/prism_har_capture.md',
            'prism-qa/QAProxyMVP/QAProxyMVP/Services/GoogleDriveService.swift': 'prism-qa/.claude/prism_export.md',
            'prism-qa/QAProxyMVP/QAProxyMVP/Services/NativeImportServer.swift': 'prism-qa/.claude/prism_mobile_web.md',
        }
        for path, doc in cases.items():
            with self.subTest(path=path):
                self.assertIn(doc, service_context.topics('prism-qa', [path]))
        self.assertEqual(service_context.topics('prism-qa', ['prism-qa/README.md']), [])

    def test_real_reference_files_exist_for_every_profile(self):
        for service, profile in service_context.PROFILES.items():
            with self.subTest(service=service):
                path = Path(review.__file__).resolve().parent / 'references' / profile['reference']
                self.assertIn(service, path.read_text().splitlines()[0])


if __name__ == '__main__':
    unittest.main()
