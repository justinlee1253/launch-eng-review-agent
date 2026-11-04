"""One crew notice per availability transition, never a stale queued offline notice."""
from contextlib import closing
from datetime import datetime
import json
import re
import queue
import sqlite3
import subprocess
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


def laptop_cycle():
    # Read only at startup/wake, never on every two-second poll.
    result = subprocess.run(['/usr/sbin/sysctl', '-n', 'kern.bootsessionuuid', 'kern.waketime', 'kern.boottime'],
        capture_output=True, timeout=1, check=True)
    text = result.stdout.decode()
    boot = re.search(r'(?m)^[0-9A-Fa-f-]{36}$', text)
    times = re.findall(r'sec = (\d+), usec = (\d+)', text)
    if not boot or len(times) != 2:
        raise ValueError('Laptop boot/wake identity unavailable')
    seconds, micros = max((int(sec), int(usec)) for sec, usec in times)
    return f'{boot[0]}:{seconds}:{micros}'


class Presence:
    def __init__(self, send, sequence=0, state_path=None):
        self.send, self.sequence = send, sequence
        self.online, self.attempted = False, False
        self.sleeping = threading.Event()
        self.lock = threading.Lock()
        self.events = queue.SimpleQueue()
        self.request_id, self.next_attempt = str(uuid.uuid4()), 0
        self.failed = False
        self.state_path, self.receipt = state_path, None

    def _save(self):
        with closing(sqlite3.connect(self.state_path, timeout=.25)) as db, db:
            db.execute('insert or replace into metadata values(?,?)',
                       ('availability_cycle', json.dumps(self.receipt)))

    def _load_cycle(self):
        if self.state_path is None or self.receipt is not None:
            return
        cycle = laptop_cycle()
        with closing(sqlite3.connect(self.state_path, timeout=.25)) as db, db:
            row = db.execute("select value from metadata where key='availability_cycle'").fetchone()
            last_online = db.execute("select max(at) from events where code='availability_online_sent'").fetchone()[0]
            last_offline = db.execute("select max(at) from events where code='availability_offline_sent'").fetchone()[0]
        previous = json.loads(row[0]) if row else {}
        if previous.get('cycle') == cycle:
            self.receipt = previous
        else:
            already_announced = (not previous and last_online is not None
                and (last_offline is None or last_online > last_offline)
                and datetime.fromisoformat(last_online.replace('Z', '+00:00')).timestamp() >= int(cycle.split(':')[-2]))
            self.receipt = {'cycle': cycle, 'online': already_announced, 'offline': False,
                            'sequence': previous.get('sequence', self.sequence - 1) + 1}
        self.sequence = self.receipt['sequence']
        self.request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'orbit-availability:' + cycle))
        self.online = bool(self.receipt['online'])
        self.attempted = self.online and not self.receipt['offline']
        self._save()

    def _send(self, online):
        lines = ONLINE if online else OFFLINE
        request_id = self.request_id if online else str(uuid.uuid5(uuid.NAMESPACE_URL, self.request_id + ':offline'))
        try:
            delivered = self.send(lines[self.sequence % len(lines)], request_id)
        except Exception:
            delivered = False
        if delivered and self.receipt is not None:
            self.receipt['online' if online else 'offline'] = True
            try:
                self._save()
            except sqlite3.Error:
                self.events.put('availability_receipt_save_failed')
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
            try:
                self._load_cycle()
            except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
                if not self.failed:
                    self.events.put('availability_cycle_unavailable')
                self.failed = True
                self.next_attempt = time.monotonic() + 30
                return
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
        if self.state_path is not None:
            self.receipt = None
            self.online, self.attempted = False, False
            self.next_attempt = 0
        self.sleeping.clear()


def deliver(value):
    import re
    import requests
    if not re.fullmatch(r'spaces/[A-Za-z0-9_-]+', value['space']) or value['text'] not in ONLINE + OFFLINE:
        return False
    with requests.post('https://chat.googleapis.com/v1/' + value['space'] + '/messages',
            headers={'Authorization': 'Bearer ' + value['token']}, json={'text': value['text']},
            params={'requestId': value['request_id']}, timeout=(1, 1), allow_redirects=False, stream=True) as response:
        if not 200 <= response.status_code < 300:
            raise requests.HTTPError(response=response)
        return True


def delivery_outcome(value):
    import requests
    try:
        return 'sent' if deliver(value) else 'invalid_request'
    except requests.Timeout:
        return 'network_timeout'
    except requests.ConnectionError:
        return 'connection_failed'
    except requests.HTTPError as error:
        return 'http_' + str(error.response.status_code)
    except Exception:
        return 'helper_failed'


if __name__ == '__main__':
    import sys
    try:
        outcome = delivery_outcome(json.load(sys.stdin))
    except Exception:
        outcome = 'invalid_request'
    print(outcome)
    raise SystemExit(0 if outcome == 'sent' else 1)
