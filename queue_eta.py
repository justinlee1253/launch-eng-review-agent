"""Cheap queue snapshots and empirical, explicitly provisional completion estimates."""

from datetime import datetime
import math
from statistics import median
import time
from zoneinfo import ZoneInfo

import usage


def _seconds(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def _timing(store, mode):
    if mode == 'stamp':
        return 20, 60, 180, False
    rows = store.db.execute("""select j.id, sum((julianday(r.finished_at)-julianday(r.started_at))*86400) as seconds
        from jobs j join model_runs r on r.job=j.id
        where j.status='submitted' and j.mode=? and r.status='completed'
        and r.model=? and r.effort=? and r.service_tier=? and r.finished_at is not null
        group by j.id order by j.updated_at desc limit 20""",
        (mode, usage.MODEL, usage.EFFORT, usage.SERVICE_TIER)).fetchall()
    samples = [max(30, row['seconds']) + 60 for row in rows if row['seconds'] is not None]
    # Cold-start prior; replace it gradually as successful end-to-end reviews accumulate.
    count = min(5, len(samples))
    expected = (median(samples) * count + 480 * (5-count)) / 5 if samples else 480
    low = max(60, min([expected * .65] + samples))
    high = max(expected * 1.75, max(samples, default=0))
    if count < 5:
        high = max(900, high)
    return low, expected, high, count < 5


def _elapsed(store, job, now):
    runs = store.db.execute('select started_at,finished_at from model_runs where job=? order by started_at', (job['id'],)).fetchall()
    start, last = _seconds(job['updated_at']), None
    for run in runs:
        began = _seconds(run['started_at'])
        if last is None or began-last > 120:
            start = began
        last = _seconds(run['finished_at']) if run['finished_at'] else now
    if last is not None and _seconds(job['updated_at'])-last > 120:
        start = _seconds(job['updated_at'])
    return max(0, now-start)


def estimate(store, job_id, now=None):
    now = time.time() if now is None else now
    rows = [dict(r) for r in store.db.execute("""select * from jobs
        where status in ('reviewing','submitting','ambiguous','queued','waiting_ci')
        order by case when status='reviewing' then 0 when status in ('submitting','ambiguous') then 1
                      when ready_after<=? then 2 else 3 end,requested_at,rowid""", (now,))]
    target = next((r for r in rows if r['id'] == job_id), None)
    if not target:
        return None
    if target['status'] == 'waiting_ci':
        return {'pending': 'CI'}
    rows = [r for r in rows if r['status'] != 'waiting_ci']
    position = next(i+1 for i, r in enumerate(rows) if r['id'] == job_id)
    result = {'position': position, 'total': len(rows), 'low': now, 'expected': now, 'high': now, 'provisional': False}
    capacity_row = store.db.execute("select value from metadata where key='worker_count'").fetchone()
    capacity = 2 if capacity_row and capacity_row[0] == '2' else 1
    slots = [dict(low=now, expected=now, high=now) for _ in range(capacity)]
    timings = {}
    for index, row in enumerate(rows[:position]):
        slot = slots[index] if row['status'] in {'reviewing','submitting','ambiguous'} and index < capacity else min(slots, key=lambda lane: lane['expected'])
        if row['status'] == 'ambiguous' or slot['expected'] == math.inf:
            slot.update(low=math.inf, expected=math.inf, high=math.inf)
            if row['id'] == job_id:
                result['pending'] = 'verification'
                return result
            continue
        if row['mode'] not in timings:
            timings[row['mode']] = _timing(store, row['mode'])
        low, expected, high, provisional = timings[row['mode']]
        if row['status'] == 'submitting':
            low, expected, high = 10, 30, 120
        elif row['status'] == 'reviewing':
            elapsed = _elapsed(store, row, now)
            overdue = elapsed >= high
            low, expected, high = max(30, low-elapsed), max(60, expected-elapsed), max(180, high-elapsed)
            if overdue:
                expected, high = max(expected, 120), max(high, 300)
        for key, duration in [('low', low), ('expected', expected), ('high', high)]:
            slot[key] = max(slot[key], row['ready_after']) + duration
            if row['id'] == job_id:
                result[key] = slot[key]
        result['provisional'] |= provisional
    return result


def describe(store, job_id, now=None):
    now = time.time() if now is None else now
    data = estimate(store, job_id, now)
    if not data:
        return ''
    if data.get('pending'):
        position = f"#{data['position']} / {data['total']}" if 'position' in data else 'waiting for CI'
        return f"\n*Queue: {position}*\n*Expected finish: pending {data['pending']}*\n*Likely range: unavailable*"
    def clock(value):
        zone = ZoneInfo('America/New_York')
        date = datetime.fromtimestamp(math.ceil(value/60)*60, zone)
        prefix = date.strftime('%a ') if date.date() != datetime.fromtimestamp(now, zone).date() else ''
        return prefix + date.strftime('%I:%M %p').lstrip('0')
    return (f"\n*Queue: #{data['position']} / {data['total']}*"
            f"\n*Expected finish: ~{clock(data['expected'])} ET*"
            f"\n*Likely range: {clock(data['low'])}–{clock(data['high'])} ET*"
            + ('\nProvisional estimate — limited timing history.' if data['provisional'] else '')
            + '\nEstimate assumes preflight/CI pass.')
