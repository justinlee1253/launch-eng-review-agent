"""One crew notice per availability transition, never a stale queued offline notice."""
import queue
import threading
import time
import uuid


ONLINE = (
    '🟢 Crew, we’re back on station. Ready to take PRs.',
    '🟢 Preflight complete. Orbit is ready for reviews.',
    '🟢 All systems checked. Send through your PRs.',
    '🟢 Back in the seat. Review queue is open.',
    '🟢 Crew, we’re operational. Standing by for PRs.',
)
OFFLINE = (
    '🌙 Crew, we’re parked for now. Reviews are paused.',
    '🌙 Shutting down for now. We’ll pick up reviews when we’re back.',
    '🌙 Off station. Reviews will resume when Orbit returns.',
    '🌙 Engines shut down. Review queue is on hold.',
    '🌙 Signing off, crew. We’ll take the next PRs when we’re back online.',
)


class Presence:
    def __init__(self, send, sequence=0):
        self.send, self.sequence = send, sequence
        self.online, self.attempted = False, False
        self.sleeping = threading.Event()
        self.lock = threading.Lock()
        self.events = queue.SimpleQueue()
        self.request_id, self.next_attempt = str(uuid.uuid4()), 0
        self.failed = False

    def _send(self, online):
        lines = ONLINE if online else OFFLINE
        request_id = self.request_id if online else str(uuid.uuid5(uuid.NAMESPACE_URL, self.request_id + ':offline'))
        try:
            delivered = self.send(lines[self.sequence % len(lines)], request_id)
        except Exception:
            delivered = False
        if delivered:
            self.events.put('availability_online_sent' if online else 'availability_offline_sent')
        elif not self.failed:
            self.events.put('availability_notice_failed')
        self.failed = not delivered
        return delivered

    def ready(self):
        if self.sleeping.is_set() or self.online or time.monotonic() < self.next_attempt:
            return
        with self.lock:
            if self.sleeping.is_set() or self.online:
                return
            self.attempted = True
            self.online = self._send(True)
            self.next_attempt = time.monotonic() + 30

    def offline(self):
        # Notice writes use a cached token and short timeouts. Never retry an offline
        # message after wake, where it would incorrectly follow a new online notice.
        with self.lock:
            if not self.attempted:
                return
            self._send(False)
            self.online, self.attempted = False, False
            self.sequence += 1
            self.request_id, self.next_attempt = str(uuid.uuid4()), 0

    def sleep(self):
        self.sleeping.set()
        self.offline()

    def wake(self):
        self.sleeping.clear()


def deliver(value):
    import re
    import requests
    if not re.fullmatch(r'spaces/[A-Za-z0-9_-]+', value['space']) or value['text'] not in ONLINE + OFFLINE:
        return False
    with requests.post('https://chat.googleapis.com/v1/' + value['space'] + '/messages',
            headers={'Authorization': 'Bearer ' + value['token']}, json={'text': value['text']},
            params={'requestId': value['request_id']}, timeout=(1, 1), allow_redirects=False, stream=True) as response:
        return 200 <= response.status_code < 300


if __name__ == '__main__':
    import json
    import sys
    try:
        success = deliver(json.load(sys.stdin))
    except Exception:
        success = False
    raise SystemExit(0 if success else 1)
