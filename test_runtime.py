from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import agent
from test_agent import SPACE, START, END, message
from test_review import GitHub, result


class RuntimeTests(unittest.TestCase):
    def test_transaction_failure_keeps_checkpoint_and_queue_consistent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = agent.Store(Path(directory) / 'state.sqlite3')
            try:
                with patch.object(store, '_outbox', side_effect=RuntimeError('simulated crash')):
                    with self.assertRaises(RuntimeError):
                        store.ingest(agent.Config(SPACE, START), [message()], START, END, None)
                self.assertEqual(store.history(), [])
                self.assertIsNone(store.meta('checkpoint'))
            finally:
                store.db.close()

    def test_exact_message_resource_proves_space_even_when_optional_space_field_is_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = agent.Store(Path(directory) / 'state.sqlite3')
            try:
                msg = message()
                del msg['space']
                store.ingest(agent.Config(SPACE, START), [msg], START, END, None)
                self.assertEqual(len(store.history()), 1)
            finally:
                store.db.close()

    def test_401_refreshes_once_without_reauthorizing_and_uses_fixed_thread(self):
        class Credentials:
            valid, token = True, 'fake-test-token'
        class Manager:
            calls = []
            def load(inner, force_refresh=False):
                inner.calls.append(force_refresh)
                return Credentials()
        class Response:
            def __init__(inner, status):
                inner.status_code = status
            def json(inner):
                return {}
        class Session:
            requests = []
            def request(inner, method, url, **options):
                inner.requests.append((method, url, options))
                return Response(401 if len(inner.requests) == 1 else 200)
        manager = Manager()
        chat = agent.Chat(agent.Config(SPACE, START), manager)
        chat.session = Session()
        chat.list_messages(START, END, 'page2')
        self.assertEqual(manager.calls, [False, True])
        self.assertEqual(chat.session.requests[1][1], 'https://chat.googleapis.com/v1/spaces/AAQAixK3auw/messages')
        self.assertEqual(chat.session.requests[1][2]['params']['pageToken'], 'page2')
        with self.assertRaises(agent.ConfigError):
            chat.request('GET', 'spaces/other/messages')

    def test_listener_acknowledges_second_request_while_first_review_is_blocked(self):
        entered, release = threading.Event(), threading.Event()
        handlers, acknowledgments, observed = {}, [], []
        class Chat:
            polls = 0
            def __init__(inner, config):
                pass
            def list_messages(inner, start, end, token):
                inner.polls += 1
                # Fixed cutoffs exclude future messages, as the real API does.
                return {'messages': [message(str(inner.polls), createTime=end)]}
            def send(inner, row):
                if row['kind'] == 'message':
                    acknowledgments.append(row['id'])
                    if len(acknowledgments) == 2:
                        observed.append(entered.is_set() and not release.is_set())
                        handlers[agent.signal.SIGTERM]()
                        release.set()
                return {'name': SPACE + '/messages/reply'}
        class Reviewer:
            def __init__(inner, directory, stop=None):
                inner.stop = stop
            def review(inner, *args):
                entered.set()
                release.wait(8)
                return result()
        def signal_handler(sig, callback):
            handlers[sig] = callback
        def stop_if_broken():
            if agent.signal.SIGTERM in handlers:
                handlers[agent.signal.SIGTERM]()
            release.set()
        timer = threading.Timer(8, stop_if_broken)
        timer.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                with patch('agent.Chat', Chat), patch('agent.signal.signal', side_effect=signal_handler), \
                     patch('review.GitHub', side_effect=lambda *args, **kwargs: GitHub()), patch('review.Reviewer', Reviewer):
                    agent.run(agent.Config(SPACE, START), directory)
                self.assertEqual(observed, [True])
                self.assertGreaterEqual(len(set(acknowledgments)), 2)
        finally:
            timer.cancel()
            release.set()


if __name__ == '__main__':
    unittest.main()
