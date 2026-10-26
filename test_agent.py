import json
import tempfile
import unittest
from pathlib import Path

import agent


SPACE = 'spaces/AAQAixK3auw'
START = '2026-10-26T18:50:00Z'
END = '2026-10-26T18:55:00Z'


def message(id='one', **changes):
    value = {'name': SPACE + '/messages/' + id, 'space': {'name': SPACE},
             'thread': {'name': SPACE + '/threads/thread1'},
             'sender': {'name': 'users/teammate', 'type': 'HUMAN'},
             'createTime': '2026-10-26T18:52:00Z',
             'text': 'Please review https://github.com/ROKT/launch-engineering/pull/6000/files?x=1#diff-a'}
    value.update(changes)
    return value


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = agent.Config(SPACE, START)
        self.store = agent.Store(Path(self.tmp.name) / 'state.sqlite3')
        self.addCleanup(self.store.db.close)

    def test_only_exact_supported_github_urls_qualify(self):
        text = ('https://github.com/ROKT/catalog/pull/6000/changes#abc '
                'https://github.com/ROKT/launch-engineering/pull/12 '
                'https://github.com.evil.test/ROKT/catalog/pull/1 '
                'https://evil.test/?next=https://github.com/ROKT/catalog/pull/1 '
                'https://github.com/outsider/repo/pull/1 '
                'https://github.com@evil.test/ROKT/catalog/pull/1')
        self.assertEqual(agent.pr_links(text), [('ROKT/launch-engineering', 12)])

    def test_wrong_space_own_bot_and_old_messages_are_ignored(self):
        bad = [message(space={'name': 'spaces/other'}),
               message(sender={'name': self.config.self_user, 'type': 'HUMAN'}),
               message(sender={'name': 'users/bot', 'type': 'BOT'}),
               message(createTime='2026-10-26T18:49:59Z'),
               message(thread={'name': 'spaces/other/threads/thread1'})]
        self.store.ingest(self.config, bad, START, END, None)
        self.assertEqual(self.store.history(), [])

    def test_pagination_and_queue_are_committed_together_and_survive_restart(self):
        self.store.ingest(self.config, [message()], START, END, 'page2')
        reopened = agent.Store(self.store.path)
        self.addCleanup(reopened.db.close)
        self.assertEqual(reopened.meta('scan')['token'], 'page2')
        self.assertIsNone(reopened.meta('checkpoint'))
        self.assertEqual(reopened.history()[0]['status'], 'queued')
        reopened.ingest(self.config, [message('two')], START, END, None)
        self.assertEqual(len(reopened.history()), 1)
        self.assertEqual(reopened.meta('checkpoint'), END)
        self.assertEqual(reopened.db.execute('select count(*) from sources').fetchone()[0], 2)

    def test_same_timestamp_messages_and_duplicate_page_do_not_duplicate_jobs_or_ack(self):
        for _ in range(2):
            self.store.ingest(self.config, [message(), message('two')], START, END, None)
        self.assertEqual(len(self.store.history()), 1)
        ack = self.store.db.execute("select * from outbox where kind='message'").fetchall()
        self.assertEqual(len(ack), 2)
        self.assertNotIn('Please review', self.store.path.read_bytes().decode('latin1'))

    def test_ack_retry_reuses_id_and_thread_after_lost_response(self):
        self.store.ingest(self.config, [message()], START, END, None)
        delivered = {}
        class Chat:
            def send(inner, row):
                payload = json.loads(row['payload'])
                if row['kind'] == 'message':
                    self.assertEqual(payload['thread']['name'], SPACE + '/threads/thread1')
                delivered[row['id']] = payload
                if len(delivered) == 1 and not getattr(inner, 'failed', False):
                    inner.failed = True
                    raise agent.TemporaryFailure('lost_response')
                return {'name': SPACE + '/messages/ack', 'createTime': END}
        chat = Chat()
        agent.flush_outbox(self.store, chat, now=0)
        agent.flush_outbox(self.store, chat, now=1000)
        messages = [v for v in delivered.values() if 'text' in v]
        self.assertEqual(len(messages), 1)
        self.assertEqual(self.store.db.execute("select status from outbox where kind='message'").fetchone()[0], 'sent')

    def test_auth_failure_is_recorded_once_and_stops_subsequent_chat_calls(self):
        class Chat:
            calls = 0
            def list_messages(inner, *args):
                inner.calls += 1
                raise agent.ChatBlocked('authorization_invalid')
        chat = Chat()
        agent.scan_once(self.config, self.store, chat, END)
        agent.scan_once(self.config, self.store, chat, END)
        self.assertEqual(chat.calls, 1)
        self.assertTrue(self.store.meta('chat_blocked'))
        self.assertEqual(self.store.db.execute("select count(*) from events where code='authorization_invalid'").fetchone()[0], 1)
        self.assertIsNone(self.store.meta('checkpoint'))

    def test_single_process_lock_rejects_second_instance(self):
        with agent.process_lock(Path(self.tmp.name) / 'worker.lock'):
            with self.assertRaises(agent.ConfigError):
                with agent.process_lock(Path(self.tmp.name) / 'worker.lock'):
                    self.fail('a second worker acquired the lock')

    def test_scan_resumes_exact_window_not_current_time(self):
        self.store.ingest(self.config, [], START, END, 'page2')
        seen = []
        class Chat:
            def list_messages(inner, start, end, token):
                seen.append((start, end, token))
                return {}
        agent.scan_once(self.config, self.store, Chat(), '2026-10-27T00:00:00Z')
        self.assertEqual(seen, [(START, END, 'page2')])
        self.assertEqual(self.store.meta('checkpoint'), END)


if __name__ == '__main__':
    unittest.main()
