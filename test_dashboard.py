import importlib.util
import json
import re
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent import Store


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('dashboard'), 'The local dashboard has not been implemented.')
        import dashboard
        self.dashboard = dashboard
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = Path(self.tmp.name)
        store = Store(self.runtime / 'state.sqlite3')
        with store.db:
            for id, number, status, requested in [('later', 22, 'queued', '2026-10-27T18:01:00Z'),
                                                   ('active', 21, 'reviewing', '2026-10-27T18:00:00Z'),
                                                   ('held', 20, 'needs-attention', '2026-10-27T17:00:00Z')]:
                store.db.execute('''insert into jobs(id,repo,number,status,requested_at,updated_at,head,mode,payload,result)
                    values(?,?,?,?,?,?,?,?,?,?)''', (id, 'ROKT/launch-engineering', number, status, requested,
                    requested, 'a'*40, 'review', 'PRIVATE_PAYLOAD' if status == 'needs-attention' else None,
                    'Finding 1: citation remains unverified.'))
            store._meta('heartbeat', '2026-10-27T18:01:50Z')
            store._meta('stamp_identities', {'private': 'PRIVATE_EMAIL'})
            store.db.execute("insert into events(at,job,code,unique_key) values(?,?,?,?)",
                             ('2026-10-27T18:01:00Z', 'held', 'needs-attention', 'PRIVATE_UNIQUE_KEY'))
            store.db.execute('''insert into model_runs(id,job,status,started_at,finished_at,model,effort,
                service_tier,estimated_credits,input_tokens,output_tokens) values(?,?,?,?,?,?,?,?,?,?,?)''',
                ('run', 'held', 'completed', '2026-10-27T17:00:00Z', '2026-10-27T17:01:00Z', 'gpt-6.1-sol',
                 'xhigh', 'fast', 2.5, 1000, 200))
            store.db.execute("insert into model_runs(id,job,status,started_at) values('active-run','active','running','2026-10-27T18:00:00Z')")
            store.db.execute("insert into model_runs(id,job,status,started_at,finished_at) values('unreported','held','failed','2026-10-27T17:02:00Z','2026-10-27T17:03:00Z')")
        store.db.close()
        self.now = 1793124120  # 2026-10-27 18:02 UTC

    def test_snapshot_orders_queue_and_reports_usage_and_exact_hold(self):
        data = self.dashboard.snapshot(self.runtime, now=self.now)
        self.assertEqual([j['number'] for j in data['queue']], [21, 22])
        self.assertEqual(data['queue'][1]['eta']['position'], 2)
        self.assertEqual(data['queue'][0]['elapsed_seconds'], 120)
        self.assertEqual(data['health']['state'], 'healthy')
        self.assertEqual(data['costs']['known_credits'], 2.5)
        self.assertEqual(data['costs']['running_runs'], 1)
        self.assertEqual(data['costs']['unreported_runs'], 1)
        self.assertEqual(data['history'][0]['unreported_runs'], 1)
        self.assertEqual(data['history'][0]['result'], 'Finding 1: citation remains unverified.')
        self.assertEqual(data['events'][0]['code'], 'needs-attention')

    def test_dashboard_connection_rejects_writes_and_does_not_export_private_fields(self):
        connect = sqlite3.connect
        def guarded(*args, **options):
            db = connect(*args, **options)
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('delete from jobs')
            db.rollback()
            return db
        with patch.object(self.dashboard.sqlite3, 'connect', side_effect=guarded):
            data = self.dashboard.snapshot(self.runtime, now=self.now)
        encoded = json.dumps(data)
        for secret in ('PRIVATE_PAYLOAD', 'PRIVATE_EMAIL', 'PRIVATE_UNIQUE_KEY'):
            self.assertNotIn(secret, encoded)

    def test_missing_database_is_not_created(self):
        missing = self.runtime / 'missing'
        with self.assertRaises(self.dashboard.Unavailable):
            self.dashboard.snapshot(missing)
        self.assertFalse(missing.exists())

    def test_stale_heartbeat_is_not_reported_as_healthy(self):
        data = self.dashboard.snapshot(self.runtime, now=self.now + 120)
        self.assertEqual(data['health']['state'], 'stale')

    def test_http_reads_live_changes_but_rejects_foreign_origins_hosts_and_writes(self):
        server = self.dashboard.server(self.runtime, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = 'http://127.0.0.1:' + str(server.server_port)
        with urlopen(url) as response:
            page = response.read()
            self.assertIn(b'LE Review', page)
            token = re.search(rb'name="review-action" content="([^"]+)"', page)[1].decode()
            self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
        with urlopen(url + '/api/state') as response:
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertEqual(len(json.load(response)['queue']), 2)
        db = sqlite3.connect(self.runtime / 'state.sqlite3')
        db.execute("update jobs set status='submitted' where id='active'")
        db.commit()
        db.close()
        with urlopen(url + '/api/state') as response:
            self.assertEqual(len(json.load(response)['queue']), 1)
        for request in (Request(url + '/api/state', headers={'Origin': 'https://outside.example'}),
                        Request(url + '/api/state', headers={'Host': 'outside.example'}),
                        Request(url + '/api/state', method='POST'),
                        Request(url + '/.runtime/state.sqlite3')):
            with self.subTest(request=request.full_url), self.assertRaises(HTTPError) as failure:
                urlopen(request)
            self.assertIn(failure.exception.code, (403, 404, 405))
            failure.exception.close()
        payload = json.dumps({'action': 'remove', 'job_id': 'later'}).encode()
        for origin, action_token in [('https://outside.example', token), (url, 'wrong-token')]:
            with self.assertRaises(HTTPError) as failure:
                urlopen(Request(url + '/api/action', data=payload, headers={
                    'Origin': origin, 'X-Review-Action': action_token, 'Content-Type': 'application/json'}))
            self.assertEqual(failure.exception.code, 403)
            failure.exception.close()
        with urlopen(Request(url + '/api/action', data=payload, headers={
                'Origin': url, 'X-Review-Action': token, 'Content-Type': 'application/json'})) as response:
            self.assertEqual(json.load(response)['removed'], 1)
        with urlopen(url + '/api/state') as response:
            self.assertEqual(json.load(response)['queue'], [])


if __name__ == '__main__':
    unittest.main()
