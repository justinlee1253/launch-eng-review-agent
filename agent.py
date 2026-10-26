"""One-space intake, durable queue/outbox, and local background supervision."""

import argparse
import contextlib
import fcntl
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import signal
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import requests

from auth import AuthRequired, ConfigError, CredentialManager, TemporaryAuthError
import usage


ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / '.runtime'
REPOS = ('ROKT/launch-engineering',)
SELF_USER = 'users/104386993234033491963'
STAMP_EMAIL = 'mani.vedma@rokt.com'
REACTIONS = {'picked_up': '👀', 'approved': '✅', 'changes_requested': '💬', 'warning': '⚠️'}
LOG = logging.getLogger('review-agent')


def utc(value=None):
    return (value or datetime.now(timezone.utc)).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def timestamp(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Timestamp requires a timezone')
    return result


@dataclass(frozen=True)
class Config:
    space: str
    activated_at: str
    self_user: str = SELF_USER
    poll_seconds: int = 2
    actor: str = 'justinlee1253'
    context_repositories: tuple = REPOS
    reactions: dict = field(default_factory=lambda: dict(REACTIONS))

    def __post_init__(self):
        if not re.fullmatch(r'spaces/[A-Za-z0-9_-]+', self.space):
            raise ConfigError('An exact Google Chat space resource is required.')
        timestamp(self.activated_at)
        if self.poll_seconds != 2 or self.actor != 'justinlee1253' or self.self_user != SELF_USER:
            raise ConfigError('This pilot uses the agreed two-second interval and Justin identities.')
        if (not isinstance(self.reactions, dict) or set(self.reactions) != set(REACTIONS)
                or any(not isinstance(v, str) or not v.strip() or len(v) > 16
                       or any(ord(c) < 32 for c in v) for v in self.reactions.values())):
            raise ConfigError('Configure one Unicode emoji for each of picked_up, approved, changes_requested, warning.')
        if not set(self.context_repositories).issubset(REPOS):
            raise ConfigError('Only ROKT/launch-engineering is currently enabled for reviews and context.')

    @classmethod
    def load(cls, path=RUNTIME / 'config.json'):
        try:
            return cls(**json.loads(Path(path).read_text()))
        except (OSError, ValueError, TypeError):
            raise ConfigError('Configure the exact dedicated space before starting the worker.') from None


class TemporaryFailure(Exception):
    """A retryable service failure; messages and credentials must be retained."""


class ChatBlocked(Exception):
    """No more Chat requests until an explicit health check succeeds."""


def pr_candidates(text):
    found = set()
    for token in re.findall(r'https?://[^\s<>"\x00-\x1f]+', text or ''):
        try:
            url = urlsplit(token.rstrip('.,);]'))
            if url.scheme != 'https' or url.netloc.lower() != 'github.com':
                continue
            match = re.fullmatch(r'/([^/]+)/([^/]+)/pull/([1-9][0-9]*)(?:/(?:files|changes|commits))?/?', url.path)
            if not match:
                continue
            found.add(('/'.join(match.group(1, 2)), int(match[3])))
        except ValueError:
            continue
    return sorted(found)


def pr_links(text):
    return sorted({(repo, number) for candidate, number in pr_candidates(text)
                   for repo in REPOS if repo.casefold() == candidate.casefold()})


class Store:
    def __init__(self, path=RUNTIME / 'state.sqlite3', reactions=None):
        self.reactions = dict(reactions or REACTIONS)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        self.db = sqlite3.connect(self.path, timeout=10)
        self.path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute('pragma journal_mode=WAL')
        self.db.execute('pragma synchronous=FULL')
        self.db.executescript('''
            create table if not exists metadata(key text primary key, value text not null);
            create table if not exists jobs(
                id text primary key, repo text not null, number integer not null,
                status text not null, head text, base text, payload text, marker text,
                review_id integer, review_url text, result text, updated_at text not null);
            create table if not exists sources(
                message text, repo text, number integer, thread text not null,
                created_at text not null, job text not null references jobs(id),
                primary key(message,repo,number));
            create table if not exists outbox(
                id text primary key, kind text not null, target text not null,
                payload text not null, status text not null default 'pending',
                attempts integer not null default 0, next_at real not null default 0,
                sent_at text, response_name text);
            create table if not exists ledger(
                repo text, number integer, head text, actor text,
                review_id integer, review_url text, event text,
                primary key(repo,number,head,actor));
            create table if not exists events(
                id integer primary key, at text not null, job text,
                code text not null, unique_key text unique);
            create table if not exists model_runs(
                id text primary key, job text, started_at text, finished_at text,
                model text, effort text, service_tier text, status text,
                input_tokens integer, cached_input_tokens integer,
                output_tokens integer, reasoning_output_tokens integer,
                estimated_credits real, pricing text);
        ''')
        columns = {row['name'] for row in self.db.execute('pragma table_info(jobs)')}
        for name, definition in {'requested_at': 'text', 'ready_after': 'real not null default 0',
                                 'mode': "text not null default 'review'", 'requester': 'text',
                                 'head_moves': 'integer not null default 0'}.items():
            if name not in columns:
                self.db.execute('alter table jobs add column ' + name + ' ' + definition)
        self.db.execute('update jobs set requested_at=coalesce((select min(created_at) from sources where sources.job=jobs.id), updated_at) where requested_at is null')
        self.db.execute('create index if not exists jobs_ready on jobs(status,ready_after,requested_at)')
        self.db.execute('create index if not exists source_pr on sources(repo,number,created_at)')
        self.db.commit()

    def meta(self, key):
        row = self.db.execute('select value from metadata where key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _meta(self, key, value):
        self.db.execute('insert or replace into metadata values(?,?)', (key, json.dumps(value)))

    def event(self, code, job=None, unique=None):
        cursor = self.db.execute('insert or ignore into events(at,job,code,unique_key) values(?,?,?,?)',
                                 (utc(), job, code, unique))
        if cursor.rowcount:
            LOG.info('%s job=%s', code, job or '-')

    def block_chat(self, code):
        with self.db:
            self._meta('chat_blocked', code)
            self.event(code, unique='chat_blocked:' + code)

    def _outbox(self, key, kind, target, payload):
        id = str(uuid.uuid5(uuid.NAMESPACE_URL, key))
        self.db.execute('insert or ignore into outbox(id,kind,target,payload) values(?,?,?,?)',
                        (id, kind, target, json.dumps(payload)))

    def _reaction(self, message, emoji):
        self._outbox('reaction:' + message + ':' + emoji, 'reaction', message, {'emoji': {'unicode': emoji}})

    def start_model_run(self, job):
        id = str(uuid.uuid4())
        with self.db:
            self.db.execute('insert into model_runs(id,job,started_at,model,effort,service_tier,status,pricing) values(?,?,?,?,?,?,?,?)',
                (id, job, utc(), usage.MODEL, usage.EFFORT, usage.SERVICE_TIER, 'running', json.dumps(usage.PRICING)))
        return id

    def end_model_run(self, id, status, tokens):
        tokens = tokens if usage.valid_usage(tokens) else {}
        with self.db:
            self.db.execute('update model_runs set finished_at=?,status=?,input_tokens=?,cached_input_tokens=?,output_tokens=?,reasoning_output_tokens=?,estimated_credits=? where id=?',
                (utc(), status, tokens.get('input_tokens'), tokens.get('cached_input_tokens'), tokens.get('output_tokens'),
                 tokens.get('reasoning_output_tokens'), usage.estimate_credits(tokens), id))

    def costs(self):
        rows = [dict(row) for row in self.db.execute('select model_runs.*,jobs.repo,jobs.number from model_runs left join jobs on jobs.id=model_runs.job order by started_at')]
        by_pr = {}
        for row in rows:
            row['pricing'] = json.loads(row['pricing'])
            key = f"{row['repo']}#{row['number']}" if row['repo'] else 'configuration_checks'
            total = by_pr.setdefault(key, {'runs': 0, 'known_estimated_credits': 0, 'unknown_usage_runs': 0})
            total['runs'] += 1
            total['known_estimated_credits'] = round(total['known_estimated_credits'] + (row['estimated_credits'] or 0), 8)
            total['unknown_usage_runs'] += row['estimated_credits'] is None
        return {'known_estimated_credits': round(sum(row['estimated_credits'] or 0 for row in rows), 8),
                'unknown_usage_runs': sum(row['estimated_credits'] is None for row in rows),
                'actual_usd': None, 'billing_note': usage.PRICING['basis'], 'by_pr': by_pr, 'runs': rows}

    def first_request(self, job):
        return self.db.execute('select * from sources where repo=? and number=? order by created_at,rowid limit 1',
                               (job['repo'], job['number'])).fetchone()

    def stamp_authorized(self, sender):
        email = sender.get('email')
        if email:
            if email.casefold() == STAMP_EMAIL and re.fullmatch(r'users/[0-9]+', sender.get('name', '')):
                self._meta('stamp_identity', {'user': sender['name'], 'email': STAMP_EMAIL, 'verified_at': utc()})
                return True
            if (self.meta('stamp_identity') or {}).get('user') == sender.get('name'):
                self._meta('stamp_identity', None)
            return False
        binding = self.meta('stamp_identity') or {}
        return binding.get('email') == STAMP_EMAIL and binding.get('user') == sender.get('name')

    def ingest(self, config, messages, start, end, token):
        with self.db:
            for msg in sorted((m for m in messages if isinstance(m, dict)), key=lambda m: str(m.get('createTime', ''))):
                name = msg.get('name', '')
                thread = msg.get('thread', {}).get('name', '')
                sender = msg.get('sender', {})
                try:
                    created = timestamp(msg.get('createTime', ''))
                    in_window = max(timestamp(start), timestamp(config.activated_at)) <= created <= timestamp(end)
                except (ValueError, TypeError):
                    continue
                if (msg.get('space', {}).get('name', config.space) != config.space
                        or not re.fullmatch(re.escape(config.space) + r'/messages/[A-Za-z0-9_.-]+', name)
                        or not re.fullmatch(re.escape(config.space) + r'/threads/[A-Za-z0-9_.-]+', thread)
                        or sender.get('type') != 'HUMAN' or sender.get('name') == config.self_user
                        or not re.fullmatch(r'users/[A-Za-z0-9_-]+', sender.get('name', ''))
                        or msg.get('deleteTime') or not in_window):
                    continue
                text = msg.get('text', '')
                links = pr_links(text)
                unsupported = [repo for repo, _ in pr_candidates(text) if repo.casefold() not in {r.casefold() for r in REPOS}]
                if unsupported:
                    self._outbox('scope:' + name, 'message', thread, {'text': 'Review held — this pilot supports only ROKT/launch-engineering (including Orbit). Other repository links were not queued.', 'thread': {'name': thread}})
                    self._reaction(name, config.reactions['warning'])
                is_mani = self.stamp_authorized(sender)
                wants_stamp = bool(re.search(r'\bSTAMP\b', text, re.IGNORECASE))
                mode = 'stamp' if wants_stamp else 'review'
                if wants_stamp:
                    command = re.sub(r'https?://[^\s<>"\x00-\x1f]+', '', text).strip(' \n\r\t:;,<>').casefold()
                    if not is_mani or command not in {'stamp', 'stamp for mani'} or not links:
                        reason = ('STAMP is reserved for verified mani.vedma@rokt.com. Post without STAMP for a Falcon review.' if not is_mani
                                  else 'Use an actual GitHub PR link followed by STAMP. A folder link does not identify a PR.')
                        self._outbox('stamp_help:' + name, 'message', thread, {'text': reason, 'thread': {'name': thread}})
                        self._reaction(name, config.reactions['warning'])
                        continue
                for repo, number in links:
                    if self.db.execute('select 1 from sources where message=? and repo=? and number=?',
                                       (name, repo, number)).fetchone():
                        continue
                    first = self.first_request({'repo': repo, 'number': number})
                    job = self.db.execute("select * from jobs where repo=? and number=? and status in ('queued','reviewing','waiting_ci','submitting','ambiguous') order by requested_at limit 1",
                                          (repo, number)).fetchone()
                    if job:
                        id = job['id']
                        if job['status'] == 'waiting_ci':
                            self.db.execute('update jobs set ready_after=0 where id=?', (id,))
                    else:
                        id = str(uuid.uuid4())
                        self.db.execute('insert into jobs(id,repo,number,status,updated_at,requested_at,mode,requester) values(?,?,?,?,?,?,?,?)',
                                        (id, repo, number, 'queued', utc(), utc(created), mode, sender['name']))
                        self.event('queued', id)
                    self.db.execute('insert into sources values(?,?,?,?,?,?)',
                                    (name, repo, number, thread, msg['createTime'], id))
                    acknowledgment = 'Received — queued for ' + ('Mani STAMP approval without code review.' if mode == 'stamp' else 'Falcon review.')
                    if first:
                        acknowledgment = f"Already requested at {first['created_at']}. " + (
                            'Following the original request; no second review is queued.' if job else
                            'Checking for a new commit; the same head will reuse the original result without model tokens.')
                    self._outbox('ack:' + name, 'message', thread,
                                 {'text': acknowledgment, 'thread': {'name': thread}})
                    self._reaction(name, config.reactions['picked_up'])
            self._meta('scan', {'start': start, 'end': end, 'token': token} if token else None)
            if not token:
                self._meta('checkpoint', end)

    def history(self):
        return [dict(row) for row in self.db.execute('select * from jobs order by updated_at desc')]

    def claim(self):
        with self.db:
            row = self.db.execute("select * from jobs where status in ('submitting','ambiguous','queued','waiting_ci') and ready_after<=? order by case when status in ('submitting','ambiguous') then 0 else 1 end,requested_at,rowid limit 1", (time.time(),)).fetchone()
            if row and row['status'] in {'queued', 'waiting_ci'}:
                self.db.execute("update jobs set status='reviewing',updated_at=? where id=?", (utc(), row['id']))
            return dict(row) if row else None

    def update(self, job, **values):
        allowed = {'status', 'head', 'base', 'payload', 'marker', 'review_id', 'review_url', 'result', 'ready_after', 'head_moves'}
        if not set(values).issubset(allowed):
            raise ValueError('Invalid job update')
        values['updated_at'] = utc()
        with self.db:
            self.db.execute('update jobs set ' + ','.join(k + '=?' for k in values) + ' where id=?',
                            (*values.values(), job['id']))
        job.update(values)

    def finish(self, job, status, text, reaction='warning'):
        emoji = self.reactions[reaction]
        with self.db:
            self.db.execute('update jobs set status=?,result=?,updated_at=? where id=?', (status, text, utc(), job['id']))
            self.event(status, job['id'], unique=job['id'] + ':' + status + ':' + hashlib.sha256(text.encode()).hexdigest())
            for source in self.db.execute('select * from sources where job=?', (job['id'],)).fetchall():
                key = job['id'] + ':' + hashlib.sha256(text.encode()).hexdigest() + ':' + source['thread']
                self._outbox('result:' + key, 'message', source['thread'],
                             {'text': text, 'thread': {'name': source['thread']}})
                self._reaction(source['message'], emoji)
        job.update(status=status, result=text)

    def reviewed(self, repo, number, head, actor):
        row = self.db.execute('select * from ledger where repo=? and number=? and head=? and actor=?',
                              (repo, number, head, actor)).fetchone()
        return dict(row) if row else None

    def record_review(self, job, actor, review):
        with self.db:
            self.db.execute('insert or replace into ledger values(?,?,?,?,?,?,?)',
                            (job['repo'], job['number'], job['head'], actor, review['id'], review['html_url'], review['state']))
            self.db.execute('update jobs set review_id=?,review_url=?,updated_at=? where id=?',
                            (review['id'], review['html_url'], utc(), job['id']))

    def recover(self):
        with self.db:
            interrupted = self.db.execute("select distinct jobs.* from jobs join model_runs on model_runs.job=jobs.id where jobs.status='reviewing' and model_runs.status='running'").fetchall()
            for row in interrupted:
                self.finish(dict(row), 'needs-attention', f"{row['repo']}#{row['number']} — Review held: model run was interrupted; usage may be unreported. Inspect costs before explicitly retrying.")
            self.db.execute("update model_runs set status='interrupted',finished_at=? where status='running'", (utc(),))
            self.db.execute("update jobs set status='queued' where status='reviewing'")
            # A reserved write is reconciled, never blindly repeated after a crash.
            self.db.execute("update jobs set status='ambiguous' where status='submitting'")

    def import_previous(self):
        with self.db:
            self.db.execute('insert or ignore into ledger values(?,?,?,?,?,?,?)',
                            ('ROKT/catalog', 5962, 'bd124867ba848a9df8e0f1013900b82b17453170',
                             'justinlee1253', 5618599675,
                             'https://github.com/ROKT/catalog/pull/5962#pullrequestreview-5618599675', 'APPROVED'))


class Chat:
    def __init__(self, config, manager=None):
        self.config = config
        self.manager = manager or CredentialManager()
        self.credentials = None
        self.session = requests.Session()

    def request(self, method, resource, **kwargs):
        if not (resource == self.config.space + '/messages'
                or re.fullmatch(re.escape(self.config.space) + r'/messages/[A-Za-z0-9_.-]+/reactions', resource)):
            raise ConfigError('Chat access is restricted to the configured space.')
        for attempt in range(2):
            if not self.credentials or not self.credentials.valid or attempt:
                try:
                    self.credentials = self.manager.load(force_refresh=bool(attempt))
                except (AuthRequired, ConfigError):
                    raise ChatBlocked('google_authorization_paused') from None
                except TemporaryAuthError:
                    raise TemporaryFailure('google_renewal_transport') from None
            try:
                response = self.session.request(method, 'https://chat.googleapis.com/v1/' + resource,
                    headers={'Authorization': 'Bearer ' + self.credentials.token},
                    timeout=(5, 15), allow_redirects=False, **kwargs)
            except requests.RequestException:
                raise TemporaryFailure('chat_transport') from None
            if response.status_code == 401 and not attempt:
                continue
            if response.status_code in (401, 403):
                raise ChatBlocked('chat_access_denied_' + str(response.status_code))
            if response.status_code == 409 and resource.endswith('/reactions'):
                return {}
            if response.status_code == 429 or response.status_code >= 500:
                raise TemporaryFailure('chat_service_' + str(response.status_code))
            if not 200 <= response.status_code < 300:
                raise ConfigError('Chat request failed with HTTP ' + str(response.status_code))
            try:
                return response.json()
            except ValueError:
                raise TemporaryFailure('invalid_chat_response') from None
        raise ChatBlocked('chat_access_denied_401')

    def list_messages(self, start, end, token=None):
        params = {'pageSize': 100, 'orderBy': 'createTime ASC',
                  'filter': f'createTime > "{start}" AND createTime < "{end}"'}
        if token:
            params['pageToken'] = token
        return self.request('GET', self.config.space + '/messages', params=params)

    def send(self, row):
        payload = json.loads(row['payload'])
        if row['kind'] == 'message':
            if payload.get('thread', {}).get('name') != row['target'] or not row['target'].startswith(self.config.space + '/threads/'):
                raise ConfigError('Reply must use its original configured-space thread.')
            return self.request('POST', self.config.space + '/messages', json=payload,
                params={'requestId': row['id'], 'messageReplyOption': 'REPLY_MESSAGE_OR_FAIL'})
        return self.request('POST', row['target'] + '/reactions', json=payload)


def scan_once(config, store, chat, cutoff=None):
    if store.meta('chat_blocked'):
        return
    window = store.meta('scan')
    if not window:
        start = max(timestamp(config.activated_at), timestamp(store.meta('checkpoint') or config.activated_at) - timedelta(minutes=2))
        window = {'start': utc(start - timedelta(microseconds=1)), 'end': cutoff or utc(), 'token': None}
    for _ in range(3):
        try:
            page = chat.list_messages(window['start'], window['end'], window['token'])
            if not isinstance(page, dict) or not isinstance(page.get('messages', []), list):
                raise TemporaryFailure('invalid_chat_page')
            store.ingest(config, page.get('messages', []), window['start'], window['end'], page.get('nextPageToken'))
        except ChatBlocked as error:
            store.block_chat(str(error))
            return
        if not page.get('nextPageToken'):
            return
        window = store.meta('scan')


def flush_outbox(store, chat, now=None):
    if store.meta('chat_blocked'):
        return
    now = time.time() if now is None else now
    # One message and up to five reactions per second stay within per-space write quotas.
    rows = store.db.execute("select * from outbox where status='pending' and next_at<=? and kind='message' order by rowid limit 1", (now,)).fetchall()
    rows += store.db.execute("select * from outbox where status='pending' and next_at<=? and kind='reaction' order by rowid limit 5", (now,)).fetchall()
    for row in rows:
        try:
            result = chat.send(row)
            with store.db:
                store.db.execute("update outbox set status='sent',sent_at=?,response_name=? where id=?",
                                 (utc(), result.get('name'), row['id']))
        except ChatBlocked as error:
            store.block_chat(str(error))
            return
        except TemporaryFailure:
            with store.db:
                store.db.execute('update outbox set attempts=attempts+1,next_at=? where id=?',
                    (now + min(300, 5 * 2 ** min(row['attempts'], 6)), row['id']))
        except ConfigError:
            with store.db:
                store.db.execute("update outbox set status='needs-attention' where id=?", (row['id'],))
                store.event('chat_delivery_needs_attention', unique='delivery:' + row['id'])


@contextlib.contextmanager
def process_lock(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ConfigError('Another review agent is already running.') from None
        yield
    finally:
        os.close(fd)


def run(config, directory=RUNTIME):
    from review import GitHub, Reviewer, review_one
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with process_lock(directory / 'worker.lock'):
        store = Store(directory / 'state.sqlite3', config.reactions)
        store.import_previous()
        store.recover()
        stop = threading.Event()
        github, reviewer = GitHub(config, directory, stop), Reviewer(directory, stop)
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        handler = RotatingFileHandler(directory / 'activity.log', maxBytes=1_000_000, backupCount=3)
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        LOG.addHandler(handler)
        LOG.setLevel(logging.INFO)

        def review_loop():
            work = Store(directory / 'state.sqlite3', config.reactions)
            while not stop.is_set():
                job = work.claim()
                if not job:
                    stop.wait(1)
                    continue
                try:
                    review_one(config, work, github, reviewer, job)
                except Exception as error:
                    # Never log third-party exception text, Chat bodies, or model source context.
                    work.finish(job, 'needs-attention', f"{job['repo']}#{job['number']} — review paused ({type(error).__name__}). No automatic retry; inspect local history.")
            work.db.close()

        worker = threading.Thread(target=review_loop, name='falcon-review', daemon=True)
        worker.start()
        chat = Chat(config)
        next_scan, failures = 0, 0
        with store.db:
            store.event('worker_started')
        try:
            while not stop.is_set():
                if not worker.is_alive():
                    with store.db:
                        store.event('review_thread_failed', unique='review_thread_failed')
                    stop.set()
                    break
                try:
                    if time.monotonic() >= next_scan:
                        scan_once(config, store, chat)
                        failures = 0
                        next_scan = time.monotonic() + config.poll_seconds
                        with store.db:
                            store._meta('heartbeat', utc())
                    flush_outbox(store, chat)
                except (TemporaryFailure, ConfigError) as error:
                    failures += 1
                    next_scan = time.monotonic() + min(300, 5 * 2 ** min(failures, 6))
                    with store.db:
                        store.event(type(error).__name__, unique='service:' + type(error).__name__)
                stop.wait(1)
        finally:
            stop.set()
            worker.join(timeout=45)
            with store.db:
                store.event('worker_stopped')
            store.db.close()
            LOG.removeHandler(handler)
            handler.close()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    setup = commands.add_parser('configure')
    setup.add_argument('--space', required=True, help='Exact spaces/ID or Google Chat URL')
    commands.add_parser('run')
    commands.add_parser('status')
    commands.add_parser('history')
    commands.add_parser('costs', help='Per-PR token usage and reference credit estimates; never an invoice')
    retry = commands.add_parser('retry', help='Explicitly retry a paused job; reserved writes are reconciled only')
    retry.add_argument('job_id')
    check = commands.add_parser('check-chat', help='Explicit read-only health check; never opens a browser')
    check.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'configure':
            value = args.space
            if value.startswith('https://chat.google.com/'):
                url = urlsplit(value)
                match = re.fullmatch(r'/u/[0-9]+/app/(?:chat|space)/([A-Za-z0-9_-]+)', url.path)
                if url.netloc != 'chat.google.com' or not match:
                    raise ConfigError('Use the exact dedicated space URL.')
                value = 'spaces/' + match[1]
            config = Config(value, utc())
            RUNTIME.mkdir(mode=0o700, exist_ok=True)
            path = RUNTIME / 'config.json'
            if path.exists():
                raise ConfigError('Already configured; preserve activation and queue before changing the space.')
            path.write_text(json.dumps(asdict(config), indent=2) + '\n')
            print('Configured ' + config.space + '; no background process was started.')
            return 0
        config = Config.load()
        if args.command == 'run':
            run(config)
            return 0
        store = Store(reactions=config.reactions)
        if args.command == 'costs':
            print(json.dumps(store.costs(), indent=2))
        elif args.command == 'history':
            print(json.dumps({'jobs': store.history(),
                'reviews': [dict(r) for r in store.db.execute('select * from ledger')],
                'events': [dict(r) for r in store.db.execute('select * from events order by id desc limit 100')]}, indent=2))
        elif args.command == 'retry':
            row = store.db.execute("select * from jobs where id=? and status in ('needs-attention','deferred')", (args.job_id,)).fetchone()
            if not row:
                raise ConfigError('Only a held job may be explicitly retried.')
            if row['mode'] == 'stamp' and not row['payload']:
                raise ConfigError('Ask Mani to post a fresh STAMP request for the current head.')
            store.update(dict(row), status='ambiguous' if row['payload'] else 'queued', ready_after=0, head_moves=0)
            print('Queued for reconciliation only.' if row['payload'] else 'Queued for a fresh review.')
        elif args.command == 'status':
            print(json.dumps({'space': config.space, 'poll_seconds': config.poll_seconds, 'worker_count': 1,
                'reactions': config.reactions,
                'model': {'name': usage.MODEL, 'reasoning_effort': usage.EFFORT, 'service_tier': usage.SERVICE_TIER},
                'checkpoint': store.meta('checkpoint'),
                'scan': store.meta('scan'), 'chat_blocked': store.meta('chat_blocked'),
                'heartbeat': store.meta('heartbeat'), 'google_auth': CredentialManager().status(),
                'jobs': [dict(r) for r in store.db.execute('select status,count(*) as count from jobs group by status')],
                'delivery': [dict(r) for r in store.db.execute('select status,count(*) as count from outbox group by status')]}, indent=2))
        elif args.command == 'check-chat':
            chat = Chat(config)
            try:
                chat.list_messages(utc(datetime.now(timezone.utc) - timedelta(seconds=1)), utc())
            except ChatBlocked as error:
                store.block_chat(str(error))
                raise
            if args.resume:
                with store.db:
                    store._meta('chat_blocked', None)
                    store.event('chat_health_restored')
            print('Dedicated space read succeeded. No consent screen was opened.')
        store.db.close()
        return 0
    except (ConfigError, ChatBlocked, TemporaryFailure) as error:
        print(str(error))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
