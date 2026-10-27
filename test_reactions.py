import json
from pathlib import Path
import tempfile
import unittest

import agent
from test_agent import SPACE, START, END, message

REACTION_SCOPE = 'https://www.googleapis.com/auth/chat.messages.reactions'

class ReactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = agent.Config(SPACE, START)
        self.store = agent.Store(Path(self.tmp.name) / 'state.sqlite3')
        self.addCleanup(self.store.db.close)

    def test_latest_status_supersedes_old_pending_status_and_can_return_to_eyes(self):
        msg = message()
        with self.store.db:
            self.store._outbox('legacy-eyes', 'reaction', msg['name'], {'emoji': {'unicode': '👀'}})
            for emoji in ('⚠️', '✅', '👀'):
                self.store._reaction(msg['name'], emoji)
        rows = self.store.db.execute("select * from outbox where kind='reaction' and status='pending'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]['payload'])['emoji']['unicode'], '👀')

    def test_status_changed_during_delivery_remains_pending(self):
        self.store.ingest(self.config, [message()], START, END, None)
        store = self.store
        class Chat:
            def send(inner, row):
                if row['kind'] == 'reaction':
                    with store.db:
                        store._reaction(row['target'], '✅')
                return {'name': 'delivered'}
        agent.flush_outbox(store, Chat())
        row = store.db.execute("select * from outbox where kind='reaction' and status='pending'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(json.loads(row['payload'])['emoji']['unicode'], '✅')

    def test_resumed_review_replaces_old_parent_and_reply_warnings_with_eyes(self):
        self.store.ingest(self.config, [message('parent')], START, END, None)
        old = self.store.claim()
        self.store.finish(old, 'needs-attention', 'Draft PR')
        self.store.ingest(self.config, [message('reply')], START, END, None)
        resumed = self.store.claim()
        self.store.processing(resumed)
        rows = self.store.db.execute("select payload from outbox where kind='reaction' and status<>'superseded'").fetchall()
        self.assertEqual([json.loads(r[0])['emoji']['unicode'] for r in rows], ['👀', '👀'])
        self.store.finish(resumed, 'submitted', 'Clean', 'approved')
        rows = self.store.db.execute("select payload from outbox where kind='reaction' and status<>'superseded'").fetchall()
        self.assertEqual([json.loads(r[0])['emoji']['unicode'] for r in rows], ['✅', '✅'])

    def test_startup_converts_legacy_reactions_to_the_latest_job_status(self):
        self.store.ingest(self.config, [message()], START, END, None)
        with self.store.db:
            self.store.db.execute("delete from outbox where kind='reaction'")
            self.store.db.execute("update jobs set status='needs-attention'")
            for emoji in ('👀', '⚠️', '✅'):
                self.store._outbox('legacy:' + emoji, 'reaction', message()['name'], {'emoji': {'unicode': emoji}})
            self.store.db.execute("update outbox set status='sent' where kind='reaction'")
        self.store.recover()
        rows = self.store.db.execute("select payload from outbox where kind='reaction' and status='pending'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]['payload'])['emoji']['unicode'], '⚠️')

    def test_replacement_removes_only_own_old_status_before_adding_new_one(self):
        target = message()['name']
        own = self.config.self_user
        reactions = [
            {'name': target + '/reactions/old', 'user': {'name': own}, 'emoji': {'unicode': '👀'}},
            {'name': target + '/reactions/oldwarn', 'user': {'name': own}, 'emoji': {'unicode': '⚠️'}},
            {'name': target + '/reactions/theirs', 'user': {'name': 'users/other'}, 'emoji': {'unicode': '⚠️'}},
            {'name': target + '/reactions/unrelated', 'user': {'name': own}, 'emoji': {'unicode': '👍'}},
        ]
        chat = agent.Chat(self.config, manager=object())
        chat.credentials = type('Credentials', (), {'scopes': [REACTION_SCOPE], 'granted_scopes': None})()
        operations = []
        def request(method, resource, **options):
            operations.append((method, resource))
            if method == 'GET':
                return {'reactions': reactions}
            if method == 'DELETE':
                reactions[:] = [r for r in reactions if r['name'] != resource]
                return {}
            self.assertFalse(any(r['user']['name'] == own and r['emoji']['unicode'] in {'👀', '⚠️'} for r in reactions))
            row = {'name': target + '/reactions/new', 'user': {'name': own}, 'emoji': options['json']['emoji']}
            reactions.append(row)
            return row
        chat.request = request
        row = {'kind': 'reaction', 'target': target, 'payload': json.dumps({'emoji': {'unicode': '✅'}})}
        chat.send(row)
        chat.send(row)  # A lost successful response must not create another reaction.
        self.assertEqual([op for op in operations if op[0] == 'DELETE'],
                         [('DELETE', target + '/reactions/old'), ('DELETE', target + '/reactions/oldwarn')])
        self.assertEqual(sum(method == 'POST' for method, _ in operations), 1)
        self.assertEqual({r['name'] for r in reactions}, {target + '/reactions/theirs', target + '/reactions/unrelated', target + '/reactions/new'})

    def test_missing_delete_permission_does_not_add_a_second_status_or_block_chat(self):
        chat = agent.Chat(self.config, manager=object())
        chat.credentials = type('Credentials', (), {'scopes': [], 'granted_scopes': None})()
        target = message()['name']
        def request(method, resource, **options):
            self.assertEqual(method, 'GET')
            return {'reactions': [{'name': target + '/reactions/old', 'user': {'name': self.config.self_user}, 'emoji': {'unicode': '👀'}}]}
        chat.request = request
        with self.assertRaises(agent.ConfigError):
            chat.send({'kind': 'reaction', 'target': target, 'payload': json.dumps({'emoji': {'unicode': '✅'}})})


if __name__ == '__main__':
    unittest.main()
