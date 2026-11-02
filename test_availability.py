"""Availability changes are cheap, ordered and never trigger authorization."""
import threading
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import agent
from test_agent import SPACE, START


class AvailabilityTests(unittest.TestCase):
    def make_presence(self, outcomes=None):
        import availability
        sent = []
        outcomes = iter(outcomes or [True] * 20)
        def send(text, request_id):
            sent.append((text, request_id))
            return next(outcomes)
        return availability.Presence(send), sent

    def test_one_notice_per_transition_rotates_without_em_dashes(self):
        presence, sent = self.make_presence()
        for _ in range(5):
            presence.ready()
            presence.ready()
            presence.sleep()
            presence.sleep()
            presence.wake()
        self.assertEqual(len(sent), 10)
        self.assertEqual(len({text for text, _ in sent[::2]}), 5)
        self.assertEqual(len({text for text, _ in sent[1::2]}), 5)
        self.assertTrue(all('\u2014' not in text and '\n' not in text for text, _ in sent))
        self.assertEqual(len({key for _, key in sent}), 10)

    def test_sleep_suppresses_online_until_wake_and_a_fresh_ready_check(self):
        presence, sent = self.make_presence()
        presence.ready()
        presence.sleep()
        presence.ready()
        presence.wake()
        self.assertEqual(len(sent), 2)
        presence.ready()
        presence.offline()
        self.assertEqual(len(sent), 4)

    def test_failed_online_retries_with_same_id_and_offline_is_not_replayed(self):
        presence, sent = self.make_presence([False, True, False, True])
        with patch('availability.time.monotonic', return_value=0):
            presence.ready()
            presence.ready()
        self.assertEqual(len(sent), 1)
        with patch('availability.time.monotonic', return_value=31):
            presence.ready()
        self.assertEqual(sent[0][1], sent[1][1])
        presence.sleep()
        presence.wake()
        presence.ready()
        self.assertEqual(len(sent), 4)
        self.assertIn('🟢', sent[-1][0])

    def test_sleep_is_acknowledged_even_when_notice_callback_fails(self):
        import power_events
        watcher = power_events.Watcher(Mock(side_effect=RuntimeError('test')), Mock())
        watcher.io = SimpleNamespace(IOAllowPowerChange=Mock())
        watcher.connection = 12
        watcher.dispatch(None, 0, 0xe0000280, 42)
        watcher.io.IOAllowPowerChange.assert_called_once_with(12, 42)
        watcher.wake.assert_not_called()

    def test_announcements_are_space_scoped_bounded_and_use_cached_credentials(self):
        chat = agent.Chat(agent.Config(SPACE, START), manager=Mock())
        chat.credentials = SimpleNamespace(valid=True, token='fake-token')
        with patch('agent.subprocess.run', return_value=SimpleNamespace(returncode=0)) as post:
            self.assertTrue(chat.announce('🟢 Crew, we’re back on station. Ready to take PRs.', 'fixed-request-id'))
            options = post.call_args.kwargs
            self.assertNotIn('fake-token', ' '.join(post.call_args.args[0]))
            self.assertEqual(options['timeout'], 3)
            import json
            payload = json.loads(options['input'])
            self.assertEqual(payload['space'], SPACE)
            self.assertEqual(payload['request_id'], 'fixed-request-id')
        chat.manager.load.assert_not_called()
        chat.credentials.valid = False
        with patch('agent.subprocess.run') as post:
            self.assertFalse(chat.announce('offline', 'another-id'))
            post.assert_not_called()

    def test_notice_wall_clock_timeout_does_not_escape_or_retry(self):
        import subprocess
        chat = agent.Chat(agent.Config(SPACE, START), manager=Mock())
        chat.credentials = SimpleNamespace(valid=True, token='fake-token')
        with patch('agent.subprocess.run', side_effect=subprocess.TimeoutExpired('notice', 3)) as post:
            self.assertFalse(chat.announce('offline', 'fixed-id'))
            post.assert_called_once()
        chat.manager.load.assert_not_called()

    def test_stalled_notice_helper_is_actually_terminated(self):
        import time
        from pathlib import Path
        chat = agent.Chat(agent.Config(SPACE, START), manager=Mock())
        chat.credentials = SimpleNamespace(valid=True, token='fake-token')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'availability.py').write_text('import time; time.sleep(30)')
            started = time.monotonic()
            with patch('agent.ROOT', root):
                self.assertFalse(chat.announce('offline', 'fixed-id'))
            self.assertLess(time.monotonic() - started, 6)

    def test_helper_accepts_only_fixed_copy_and_never_follows_redirects(self):
        import availability
        value = {'space': SPACE, 'token': 'fake-token', 'text': availability.ONLINE[0], 'request_id': 'test-id'}
        response = Mock(status_code=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch('requests.post', return_value=response) as post:
            self.assertTrue(availability.deliver(value))
            self.assertEqual(post.call_args.args[0], 'https://chat.googleapis.com/v1/' + SPACE + '/messages')
            self.assertFalse(post.call_args.kwargs['allow_redirects'])
            self.assertEqual(post.call_args.kwargs['json'], {'text': value['text']})
            self.assertFalse(availability.deliver({**value, 'space': 'spaces/other/messages'}))
            self.assertFalse(availability.deliver({**value, 'text': 'untrusted text'}))
            self.assertEqual(post.call_count, 1)

    def test_auth_blocked_startup_never_announces_online(self):
        handlers, sent = {}, []
        class Client:
            def __init__(self, *args):
                pass
        class Chat:
            def __init__(self, config):
                pass
            def list_messages(self, *args):
                handlers[agent.signal.SIGTERM]()
                raise agent.ChatBlocked('test_auth_blocked')
            def announce(self, *args):
                sent.append(args)
                return True
        with tempfile.TemporaryDirectory() as root, patch('agent.Chat', Chat), \
             patch('review.GitHub', Client), patch('review.Reviewer', Client), \
             patch('power_events.Watcher'), \
             patch('agent.signal.signal', side_effect=lambda sig, callback: handlers.update({sig: callback})):
            agent.run(agent.Config(SPACE, START), root)
        self.assertEqual(sent, [])

    def test_runtime_announces_only_after_successful_scan_and_stops_offline(self):
        handlers, sent = {}, []
        ready = threading.Event()
        class Client:
            def __init__(self, *args):
                ready.set()
        class Chat:
            def __init__(self, config):
                self.read = False
            def list_messages(self, *args):
                ready.wait(2)
                self.read = True
                return {'messages': []}
            def announce(self, text, request_id):
                sent.append(text)
                self_test.assertTrue(self.read)
                if len(sent) == 1:
                    handlers[agent.signal.SIGTERM]()
                return True
        self_test = self
        with tempfile.TemporaryDirectory() as root, patch('agent.Chat', Chat), \
             patch('review.GitHub', Client), patch('review.Reviewer', Client), \
             patch('power_events.Watcher') as watcher, \
             patch('agent.signal.signal', side_effect=lambda sig, callback: handlers.update({sig: callback})):
            agent.run(agent.Config(SPACE, START), root)
            watcher.return_value.close.assert_called_once()
        self.assertEqual(len(sent), 2)
        self.assertTrue(sent[0].startswith('🟢'))
        self.assertTrue(sent[1].startswith('🌙'))


if __name__ == '__main__':
    unittest.main()
