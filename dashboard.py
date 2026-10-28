"""Loopback-only, read-only view of the local review queue and operational logs."""

import argparse
import base64
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

import queue_eta
import queue_controls

ROOT = Path(__file__).resolve().parent
ACTIVE = ('reviewing', 'submitting', 'ambiguous', 'queued', 'waiting_ci')


class Unavailable(Exception):
    pass


def snapshot(runtime, now=None):
    now = time.time() if now is None else now
    database = Path(runtime).resolve() / 'state.sqlite3'
    db = None
    try:
        db = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=0.25)
        db.row_factory = sqlite3.Row
        db.execute('pragma query_only=ON')
        db.execute('begin')
        budget = time.monotonic() + 2
        db.set_progress_handler(lambda: int(time.monotonic() > budget), 1000)
        marks = ','.join('?' for _ in ACTIVE)
        query = '''select j.id,j.repo,j.number,j.status,j.head,j.requested_at,j.updated_at,j.mode,j.result,j.review_url,
            m.started_at,m.known_credits,coalesce(m.runs,0) as runs,coalesce(m.unreported_runs,0) as unreported_runs
            from jobs j left join
            (select job,min(started_at) as started_at,sum(estimated_credits) as known_credits,count(*) as runs,
             sum(status<>'running' and estimated_credits is null) as unreported_runs
             from model_runs group by job) m on m.job=j.id '''
        queue = [dict(row) for row in db.execute(query + f'''where j.status in ({marks}) order by
            case when j.status='reviewing' then 0 when j.status in ('submitting','ambiguous') then 1
                 when j.ready_after<=? then 2 else 3 end,j.requested_at,j.rowid limit 1000''', (*ACTIVE, now))]
        history = [dict(row) for row in db.execute(query + f'''where j.status not in ({marks})
            order by j.updated_at desc,j.rowid desc limit 50''', ACTIVE)]
        has_cancellations = db.execute("select 1 from sqlite_master where name='cancellations' and type='table'").fetchone()
        for index, job in enumerate(queue):
            job['eta'] = queue_eta.estimate(SimpleNamespace(db=db), job['id'], now) if index < 20 else None
            job['elapsed_seconds'] = queue_eta._elapsed(SimpleNamespace(db=db), job, now) if job['status'] == 'reviewing' else None
            job['stop_requested'] = bool(has_cancellations and db.execute('select 1 from cancellations where job=?', (job['id'],)).fetchone())
        for job in queue + history:
            job['result'] = (job['result'] or '')[:10000]
            # URLs from state never become arbitrary links in the browser.
            repo = job['repo']
            valid_repo = isinstance(repo, str) and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo)
            job['url'] = f"https://github.com/{repo}/pull/{job['number']}" if valid_repo and type(job['number']) is int else None
            if not job['url'] or not re.fullmatch(re.escape(job['url']) + r'#pullrequestreview-[0-9]+', job['review_url'] or ''):
                job['review_url'] = None
        metadata = {}
        for row in db.execute("select key,value from metadata where key in ('heartbeat','chat_blocked','checkpoint','worker_count')"):
            metadata[row['key']] = json.loads(row['value'])
        heartbeat = metadata.get('heartbeat')
        try:
            age = max(0, now - datetime.fromisoformat(heartbeat.replace('Z', '+00:00')).timestamp())
        except (AttributeError, TypeError, ValueError):
            age = None
        health = {'state': 'paused' if metadata.get('chat_blocked') else 'unknown' if age is None
                  else 'healthy' if age <= 30 else 'stale', 'heartbeat': heartbeat, 'age_seconds': age}
        costs = dict(db.execute('''select coalesce(sum(estimated_credits),0) as known_credits,count(*) as runs,
            coalesce(sum(status='running'),0) as running_runs,
            coalesce(sum(status<>'running' and estimated_credits is null),0) as unreported_runs from model_runs''').fetchone())
        costs['actual_usd'] = None
        latest = '''with latest as (select status,row_number() over
            (partition by repo,number order by requested_at desc,rowid desc) as rank from jobs)
            select count(*) from latest where rank=1 and status in ('needs-attention','deferred','ambiguous')'''
        capacity = metadata.get('worker_count', 1)
        owned = db.execute("select 1 from pragma_table_info('jobs') where name='owner'").fetchone()
        counts = {'worker_count': capacity, 'active_workers': db.execute('select count(*) from jobs where owner is not null').fetchone()[0] if owned else sum(j['status'] in ('reviewing','submitting','ambiguous') for j in queue), 'active': db.execute(f'select count(*) from jobs where status in ({marks})', ACTIVE).fetchone()[0],
                  'attention': db.execute(latest).fetchone()[0],
                  'submitted': db.execute('select count(*) from ledger').fetchone()[0]}
        events = [dict(row) for row in db.execute('''select e.id,e.at,e.code,e.job,j.repo,j.number,
            case when j.status=e.code then substr(j.result,1,1400) else null end as detail
            from events e left join jobs j on j.id=e.job order by e.id desc limit 150''')]
        runs = [dict(row) for row in db.execute('''select r.id,r.job,r.started_at,r.finished_at,r.status,
            r.model,r.effort,r.service_tier,r.input_tokens,r.cached_input_tokens,r.output_tokens,
            r.estimated_credits,j.repo,j.number from model_runs r left join jobs j on j.id=r.job
            order by r.started_at desc limit 100''')]
        delivery = {row['status']: row['count'] for row in db.execute('select status,count(*) as count from outbox group by status')}
        return {'generated_at': datetime.fromtimestamp(now, timezone.utc).isoformat(), 'health': health,
                'counts': counts, 'costs': costs, 'queue': queue, 'history': history,
                'events': events, 'runs': runs, 'delivery': delivery, 'can_stop': queue_controls.supports_stop(db)}
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError):
        raise Unavailable('Review state is unavailable. Check the local worker and runtime path.') from None
    finally:
        if db is not None:
            db.close()


def server(runtime, port=8765):
    token = secrets.token_urlsafe(32)
    page = (ROOT / 'dashboard.html').read_bytes().replace(b'__ACTION_TOKEN__', token.encode())
    hashes = ["'sha256-" + base64.b64encode(hashlib.sha256(block).digest()).decode() + "'"
              for block in re.findall(rb'<(?:script|style)>(.*?)</(?:script|style)>', page, re.S)]
    csp = ("default-src 'none'; script-src " + ' '.join(hashes) + "; style-src " + ' '.join(hashes)
           + "; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, status, body, kind='application/json; charset=utf-8'):
            self.send_response(status)
            for name, value in {'Content-Type': kind, 'Content-Length': str(len(body)), 'Cache-Control': 'no-store',
                                'Content-Security-Policy': csp, 'X-Content-Type-Options': 'nosniff',
                                'Referrer-Policy': 'no-referrer'}.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            hosts = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            if (self.headers.get('Host') not in hosts or self.headers.get('Sec-Fetch-Site') == 'cross-site'
                    or self.headers.get('Origin') not in {None, *('http://' + host for host in hosts)}):
                self.send(403, b'{"error":"Local dashboard access only."}')
                return
            path = urlsplit(self.path).path
            if path == '/':
                self.send(200, page, 'text/html; charset=utf-8')
            elif path == '/api/state':
                try:
                    self.send(200, json.dumps(snapshot(runtime), allow_nan=False).encode())
                except Unavailable as error:
                    self.send(503, json.dumps({'error': str(error)}).encode())
            else:
                self.send(404, b'{"error":"Not found."}')

        def do_POST(self):
            origin = 'http://' + self.headers.get('Host', '')
            if (self.headers.get('Host') not in {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
                    or self.headers.get('Origin') != origin
                    or self.headers.get('Sec-Fetch-Site') not in {None, 'same-origin'}
                    or not secrets.compare_digest(self.headers.get('X-Review-Action', ''), token)):
                self.send(403, b'{"error":"Reload the local dashboard before using queue controls."}')
                return
            if self.path != '/api/action':
                self.send(404, b'{"error":"Not found."}')
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 100_000 or self.headers.get('Content-Type') != 'application/json':
                    raise ValueError()
                value = queue_controls.act(runtime, json.loads(self.rfile.read(length)))
                self.send(200, json.dumps(value).encode())
            except (ValueError, TypeError, UnicodeError):
                self.send(400, b'{"error":"Select valid queue requests."}')
            except queue_controls.Conflict as error:
                self.send(409, json.dumps({'error': str(error)}).encode())
            except Exception:
                self.send(503, b'{"error":"Queue action unavailable. Refresh to check its current state before retrying."}')

        def reject_write(self):
            self.send(405, b'{"error":"Unsupported action."}')

        do_PUT = do_PATCH = do_DELETE = reject_write

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, default=ROOT / '.runtime')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    with server(args.runtime, args.port) as dashboard:
        print(f'LE Review dashboard: http://127.0.0.1:{dashboard.server_port}', flush=True)
        try:
            dashboard.serve_forever()
        except KeyboardInterrupt:
            pass
