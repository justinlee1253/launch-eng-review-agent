"""Explicit dashboard cancellations; never delete history or undo GitHub writes."""

import json
from pathlib import Path


class Conflict(Exception):
    pass


class Cancelled(Exception):
    pass


def supports_stop(db):
    row = db.execute("select value from metadata where key='queue_controls'").fetchone()
    latest = db.execute("select max(id) from events where code='worker_started'").fetchone()[0]
    try:
        value = json.loads(row[0]) if row else {}
        return value.get('version') == 1 and latest is not None and value.get('started_event') == latest
    except (ValueError, AttributeError, TypeError):
        return False


def act(runtime, request):
    from agent import Config, Store, utc
    if not isinstance(request, dict):
        raise ValueError('Expected a queue action.')
    action = request.get('action')
    ids = request.get('job_ids') if action == 'clear' else [request.get('job_id')]
    if (action not in {'remove', 'stop', 'clear'} or not isinstance(ids, list) or not 1 <= len(ids) <= 1000
            or any(not isinstance(id, str) or not id or len(id) > 128 for id in ids)):
        raise ValueError('Select valid queue requests.')
    runtime = Path(runtime)
    if not (runtime / 'state.sqlite3').is_file():
        raise Conflict('Worker state is unavailable; nothing was removed.')
    config = Config.load(runtime / 'config.json') if (runtime / 'config.json').is_file() else None
    store = Store(runtime / 'state.sqlite3', config.reactions if config else None)
    removed, stopping, skipped = 0, 0, 0
    try:
        for id in dict.fromkeys(ids):
            with store.db:
                store.db.execute('begin immediate')
                row = store.db.execute('select * from jobs where id=?', (id,)).fetchone()
                if row and row['status'] == 'cancelled':
                    continue  # Idempotent after a lost successful dashboard response.
                if not row or row['payload'] or row['status'] in {'submitting', 'ambiguous'}:
                    if action == 'clear':
                        skipped += 1
                        continue
                    raise Conflict('Submission has begun or this request has ended. Check its current result; it was not cancelled.')
                job = dict(row)
                if action in {'remove', 'clear'}:
                    if row['status'] not in {'queued', 'waiting_ci'} or row['owner'] is not None:
                        if action == 'clear':
                            skipped += 1
                            continue
                        raise Conflict('This review already started. Refresh and use Stop review instead.')
                    store.finish(job, 'cancelled', f"{job['repo']}#{job['number']} — Cancelled from the dashboard. No GitHub review was submitted. Post the PR again to request a new review.")
                    removed += 1
                else:
                    if row['status'] != 'reviewing' or not supports_stop(store.db):
                        raise Conflict('Active stopping requires the updated worker. Refresh after it has restarted safely.')
                    inserted = store.db.execute('insert or ignore into cancellations(job,requested_at) values(?,?)', (id, utc()))
                    if inserted.rowcount:
                        store.event('cancellation_requested', id)
                    stopping += 1
        return {'removed': removed, 'stopping': stopping, 'skipped': skipped}
    finally:
        store.db.close()
