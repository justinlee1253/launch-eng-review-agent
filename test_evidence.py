import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from agent import Config
from review import Evidence, GitHub, Incomplete, Reviewer
from test_agent import SPACE, START
from test_review import BASE, HEAD, PR, result


def blob(text):
    raw = text.encode()
    return {'sha': hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest(),
            'type': 'file', 'encoding': 'base64', 'size': len(raw), 'content': base64.b64encode(raw).decode()}


class Remote(GitHub):
    def __init__(self, directory):
        super().__init__(Config(SPACE, START), directory)
        self.before, self.after = blob('return user\n'), blob('return user.name\n')
        self.calls = []
        self.mode = '100644'
        self.file_count = 1

    def local_context(self, repo):
        return {}

    def api(self, endpoint, payload=None):
        self.calls.append(endpoint)
        if '/compare/' in endpoint:
            return {'merge_base_commit': {'sha': BASE}}
        if endpoint.endswith('/pulls/6000/files?per_page=100&page=1'):
            return [{'filename': 'handler.py', 'status': 'modified', 'sha': self.after['sha'],
                     'additions': 1, 'deletions': 1, 'changes': 2, 'patch': 'truncated patch ignored'}]
        if endpoint.endswith('/pulls/6000'):
            pr = copy.deepcopy(PR)
            pr['changed_files'] = self.file_count
            return pr
        if '/git/trees/' in endpoint:
            obj = self.after if endpoint.endswith(HEAD) else self.before
            return {'truncated': False, 'tree': [{'path': 'handler.py', 'mode': self.mode, 'type': 'blob', 'sha': obj['sha']}]}
        if '/git/blobs/' in endpoint:
            return self.after if endpoint.endswith(self.after['sha']) else self.before
        if '/contents/handler.py?ref=' in endpoint:
            return self.after if endpoint.endswith(HEAD) else self.before
        if '/contents/' in endpoint:
            raise FileNotFoundError()
        if '/git/ref/heads/main' in endpoint:
            return {'object': {'sha': 'd' * 40}}
        raise AssertionError('Unexpected endpoint ' + endpoint)


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.github = Remote(self.tmp.name)

    def test_complete_diff_is_rebuilt_from_verified_blobs_not_truncated_patch(self):
        evidence = self.github.capture({'repo': 'ROKT/launch-engineering', 'number': 6000}, copy.deepcopy(PR))
        self.assertIn('-return user\n+return user.name', evidence.diff)
        self.assertIn(('handler.py', 'RIGHT', 1), evidence.inline)
        self.assertIn('ROKT/launch-engineering@' + HEAD + ':handler.py', evidence.documents)

    def test_incomplete_file_list_holds_review(self):
        pr = copy.deepcopy(PR)
        pr['changed_files'] = 2
        with self.assertRaises(Incomplete):
            self.github.capture({'repo': 'ROKT/launch-engineering', 'number': 6000}, pr)

    def test_symlink_or_submodule_cannot_be_reviewed_as_regular_file(self):
        for mode in ('120000', '160000'):
            with self.subTest(mode=mode):
                self.github.mode = mode
                with self.assertRaises(Incomplete):
                    self.github.capture({'repo': 'ROKT/launch-engineering', 'number': 6000}, copy.deepcopy(PR))

    def test_blob_corruption_or_truncation_holds_review(self):
        self.github.after['content'] = base64.b64encode(b'wrong').decode()
        with self.assertRaises(Incomplete):
            self.github.capture({'repo': 'ROKT/launch-engineering', 'number': 6000}, copy.deepcopy(PR))

    def test_evidence_cannot_read_unapproved_repository_or_traverse_path(self):
        evidence = Evidence('ROKT/launch-engineering', HEAD, BASE, BASE)
        for request in [dict(repo='ROKT/other', ref='main', kind='file', path='file.py'),
                        dict(repo='ROKT/launch-engineering', ref='head', kind='file', path='../credentials.json'),
                        dict(repo='ROKT/launch-engineering', ref='development', kind='file', path='file.py')]:
            with self.assertRaises(Incomplete):
                evidence.fulfill(self.github, [request])
        self.assertEqual(self.github.calls, [])

    def test_core_api_rejects_catalog_before_invoking_cli(self):
        from unittest.mock import patch
        github = GitHub(Config(SPACE, START), self.tmp.name)
        with patch('review.subprocess.run', side_effect=AssertionError('Out-of-scope CLI call')):
            with self.assertRaises(Incomplete):
                github.api('repos/ROKT/catalog')

    def test_approved_main_is_pinned_for_all_evidence_rounds(self):
        evidence = Evidence('ROKT/launch-engineering', HEAD, BASE, BASE)
        request = dict(repo='ROKT/launch-engineering', ref='main', kind='tree', path='')
        evidence.fulfill(self.github, [request])
        evidence.fulfill(self.github, [request])
        self.assertEqual(sum('/git/ref/heads/main' in call for call in self.github.calls), 1)
        tree_calls = [c for c in self.github.calls if '/git/trees/' in c]
        self.assertTrue(all(c.endswith('d' * 40) for c in tree_calls))

    def test_model_process_receives_no_shell_tokens_or_external_tools(self):
        executable = Path(self.tmp.name) / 'fake-codex'
        import sys
        executable.write_text('#!' + sys.executable + '\n' + '''
import json, os, pathlib, sys
args = sys.argv[1:]
assert 'GITHUB_TOKEN' not in os.environ
assert 'GOOGLE_ACCESS_TOKEN' not in os.environ
assert '--ignore-user-config' in args and '--ephemeral' in args
assert 'features.shell_tool=false' in args and 'features.apps=false' in args
assert 'agents.enabled=false' in args and 'features.plugins=false' in args
assert args[args.index('--sandbox')+1] == 'read-only'
assert args[args.index('--model')+1] == 'gpt-6.1-sol'
assert 'model_reasoning_effort="xhigh"' in args and 'service_tier="fast"' in args
sys.stdin.read()
pathlib.Path(args[args.index('--output-last-message')+1]).write_text(json.dumps({"status":"incomplete","summary":"isolated"}))
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"result"}}))
print(json.dumps({"type":"turn.completed","usage":{"input_tokens":1000,"cached_input_tokens":100,"output_tokens":200,"reasoning_output_tokens":150}}))
''')
        executable.chmod(0o700)
        reviewer = Reviewer(self.tmp.name)
        reviewer.executable = str(executable)
        from unittest.mock import patch
        with patch.dict(os.environ, {'GITHUB_TOKEN': 'test-value', 'GOOGLE_ACCESS_TOKEN': 'test-value'}):
            self.assertEqual(reviewer.invoke('smoke')['summary'], 'isolated')

        original = executable.read_text()
        for item, allowed in [
            ({'type': 'error', 'message': 'Ignoring unknown `features` requirement `ultrafast_mode` from requirements layers: managed speed setting'}, True),
            ({'type': 'error', 'message': 'Code Mode is unavailable because code-mode host is disabled. Code mode will fail closed; enable `features.code_mode_host` and install `codex-code-mode-host`.'}, True),
            ({'type': 'error', 'message': 'Unknown config setting features.shell_tool'}, False),
            ({'type': 'command_execution', 'command': 'example'}, False),
            ({'type': 'mcp_tool_call', 'tool': 'example'}, False),
        ]:
            executable.write_text(original + '\nprint(' + repr(json.dumps({'type': 'item.completed', 'item': item})) + ')\n')
            if allowed:
                self.assertEqual(reviewer.invoke('smoke')['summary'], 'isolated')
            else:
                with self.assertRaises(Incomplete):
                    reviewer.invoke('smoke')


if __name__ == '__main__':
    unittest.main()
