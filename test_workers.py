"""Deterministic ownership and message aggregation checks (no external calls)."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import agent
from test_agent import SPACE, START, END, message


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'state.sqlite3'
        self.store = agent.Store(self.path)
        self.addCleanup(self.store.db.close)
        self.config = agent.Config(SPACE, START)

    def enqueue(self, numbers=(1, 2, 3)):
        self.store.ingest(self.config, [message(str(n), text=f'https://github.com/ROKT/launch-engineering/pull/{n}')
                                      for n in numbers], START, END, None)

    def test_capacity_validation_rejects_bool_float_and_out_of_range(self):
        for value in (True, 1.0, 0, 3, '2'):
            with self.subTest(value=value), self.assertRaises(agent.ConfigError):
                agent.Config(SPACE, START, worker_count=value)
        self.assertEqual(agent.Config(SPACE, START, worker_count=2).worker_count, 2)

    def test_simultaneous_claims_exclude_duplicate_pr_through_submission(self):
        self.enqueue((1, 2))
        with self.store.db:
            self.store.db.execute("insert into jobs(id,repo,number,status,updated_at,requested_at) values('duplicate','ROKT/launch-engineering',1,'queued',?,?)", (START, START))
        barrier = threading.Barrier(2)
        claimed, failures = [], []
        def claim(owner):
            work = agent.Store(self.path)
            try:
                barrier.wait(5)
                claimed.append(work.claim(owner))
            except BaseException as error:
                failures.append(error)
            finally:
                work.db.close()
        threads = [threading.Thread(target=claim, args=(owner,)) for owner in ('one', 'two')]
        for thread in threads: thread.start()
        for thread in threads: thread.join(10)
        self.assertFalse(failures)
        self.assertEqual({j['number'] for j in claimed}, {1, 2})
        self.assertIsNone(self.store.claim('three'))
        first = next(j for j in claimed if j['number'] == 1)
        self.store.update(first, status='submitting', payload='{}')
        self.assertIsNone(self.store.claim('three'))
        impostor = dict(first, owner='wrong')
        with self.assertRaises(agent.ConfigError):
            self.store.release(impostor)
        self.store.release(first)
        recovered = self.store.claim('three')
        self.assertEqual(recovered['id'], first['id'])
        self.assertEqual(recovered['status'], 'submitting')

    def test_stale_owner_cannot_finish_or_reserve_another_workers_job(self):
        self.enqueue((1,))
        job = self.store.claim('one')
        self.store.release(job)
        with self.store.db:
            self.store.db.execute("update jobs set status='queued' where id=?", (job['id'],))
        current = self.store.claim('two')
        for operation in (lambda: self.store.finish(job, 'submitted', 'wrong', 'approved'),
                          lambda: self.store.reserve_submission(job, {}, 'wrong'),
                          lambda: self.store.record_review(job, 'justinlee1253', {'id':1,'html_url':'wrong','state':'APPROVED'})):
            with self.assertRaises(agent.ConfigError): operation()
        unowned_snapshot = dict(job, owner=None)
        with self.assertRaises(agent.ConfigError):
            self.store.finish(unowned_snapshot, 'submitted', 'wrong', 'approved')
        self.assertEqual(self.store.history()[0]['owner'], 'two')
        self.assertEqual(self.store.history()[0]['status'], 'reviewing')
        self.assertIsNone(self.store.history()[0]['payload'])

    def test_repost_attaches_while_submission_is_owned(self):
        self.enqueue((1,))
        job = self.store.claim('one')
        self.store.update(job, status='submitting', payload='{}')
        self.store.ingest(self.config, [message('repost', text='https://github.com/ROKT/launch-engineering/pull/1')], START, END, None)
        self.assertEqual(len(self.store.history()), 1)
        self.assertIsNone(self.store.claim('two'))
        self.assertEqual(self.store.db.execute('select count(*) from sources').fetchone()[0], 2)

    def emoji(self, name):
        row = self.store.db.execute("select payload from outbox where kind='reaction' and target=? and status<>'superseded'", (name,)).fetchone()
        return json.loads(row[0])['emoji']['unicode']

    def test_multi_pr_message_has_order_independent_combined_status(self):
        msg = message('multi', text='https://github.com/ROKT/launch-engineering/pull/1 https://github.com/ROKT/launch-engineering/pull/2')
        self.store.ingest(self.config, [msg], START, END, None)
        one, two = self.store.claim('one'), self.store.claim('two')
        self.store.finish(one, 'submitted', 'approved', 'approved')
        self.assertEqual(self.emoji(msg['name']), '👀')
        self.store.finish(two, 'submitted', 'changes requested', 'changes_requested')
        self.assertEqual(self.emoji(msg['name']), '💬')
        self.store.finish(one, 'needs-attention', 'held')
        self.assertEqual(self.emoji(msg['name']), '⚠️')

    def test_recovery_clears_ownership_and_never_repeats_reserved_write(self):
        self.enqueue((1, 2))
        one, two = self.store.claim('one'), self.store.claim('two')
        self.store.start_model_run(one['id'])
        self.store.update(two, status='submitting', payload='{}')
        self.store.recover()
        rows = {r['number']: r for r in self.store.history()}
        self.assertEqual(rows[1]['status'], 'needs-attention')
        self.assertEqual(rows[2]['status'], 'ambiguous')
        self.assertTrue(all(r['owner'] is None for r in rows.values()))
        self.assertEqual(self.store.claim('new')['number'], 2)

    def test_recovery_holds_completed_model_work_instead_of_resetting_its_budget(self):
        self.enqueue((1,))
        job = self.store.claim('one')
        run = self.store.start_model_run(job['id'])
        self.store.end_model_run(run, 'completed', {'input_tokens':1, 'cached_input_tokens':0,
            'output_tokens':1, 'reasoning_output_tokens':0})
        self.store.recover()
        self.assertEqual(self.store.history()[0]['status'], 'needs-attention')
        self.assertIsNone(self.store.claim('two'))

    def test_multi_pr_reused_approval_and_reverse_finish_order(self):
        msg = message('multi', text='https://github.com/ROKT/launch-engineering/pull/1 https://github.com/ROKT/launch-engineering/pull/2')
        self.store.ingest(self.config, [msg], START, END, None)
        one, two = self.store.claim('one'), self.store.claim('two')
        self.store.finish(two, 'submitted', 'changes requested', 'changes_requested')
        self.assertEqual(self.emoji(msg['name']), '👀')
        self.store.finish(one, 'skipped', 'already approved', 'approved')
        self.assertEqual(self.emoji(msg['name']), '💬')
        self.store.finish(two, 'skipped', 'already approved', 'approved')
        self.assertEqual(self.emoji(msg['name']), '✅')
        self.store.finish(two, 'waiting_ci', 'pending')
        self.assertEqual(self.emoji(msg['name']), '⚠️')

    def test_runtime_has_two_isolated_workers_and_one_listener_sender(self):
        entered = threading.Barrier(3)
        release_first, release_second, third = threading.Event(), threading.Event(), threading.Event()
        handlers, clients, send_threads, errors = {}, [], set(), []
        test = self
        class Chat:
            polls = 0
            def __init__(inner, config):
                clients.append(('chat', threading.get_ident()))
            def list_messages(inner, start, end, token):
                inner.polls += 1
                return {'messages': [message(str(n), createTime=end,
                    text=f'https://github.com/ROKT/launch-engineering/pull/{n}') for n in (1,2,3)] if inner.polls == 1 else []}
            def send(inner, row):
                send_threads.add(threading.get_ident())
                if not release_first.is_set():
                    try:
                        entered.wait(5)
                        test.assertFalse(third.is_set())
                        db = agent.Store(test.path)
                        try:
                            test.assertEqual(db.db.execute('select count(*) from jobs where owner is not null').fetchone()[0], 2)
                        finally:
                            db.db.close()
                        release_first.set()
                        test.assertTrue(third.wait(5))
                    except BaseException as error:
                        errors.append(error)
                    finally:
                        release_first.set()
                        release_second.set()
                        handlers[agent.signal.SIGTERM]()
                return {'name': SPACE + '/messages/sent'}
        class Client:
            def __init__(inner, config_or_directory, directory_or_stop=None, stop=None):
                clients.append((inner, threading.get_ident()))
        def fake_review(config, store, github, reviewer, job):
            if job['number'] in (1,2):
                entered.wait(5)
                (release_first if job['number']==1 else release_second).wait(5)
            else:
                third.set()
            store.finish(job, 'submitted', 'done', 'approved')
        with patch('agent.Chat', Chat), patch('review.GitHub', Client), patch('review.Reviewer', Client), \
             patch('review.review_one', side_effect=fake_review), \
             patch('agent.signal.signal', side_effect=lambda sig, callback: handlers.update({sig:callback})):
            agent.run(agent.Config(SPACE, START, worker_count=2), self.tmp.name)
        self.assertFalse(errors)
        self.assertTrue(third.is_set())
        self.assertEqual(len(send_threads), 1)
        self.assertEqual(sum(c[0]=='chat' for c in clients), 1)
        worker_clients = [c for c in clients if c[0]!='chat']
        self.assertEqual(len(worker_clients), 4)
        self.assertEqual(len({c[1] for c in worker_clients}), 2)
        self.assertEqual(self.store.db.execute('select count(*) from jobs where owner is not null').fetchone()[0], 0)

    def test_timeout_and_cancellation_do_not_mix_other_worker_budget_or_usage(self):
        import review
        from test_review import GitHub, result
        for failure in ('timeout', 'cancel'):
            with self.subTest(failure=failure):
                self.enqueue((6000, 6001))
                barrier = threading.Barrier(2)
                local, failures, deadlines = threading.local(), [], {}
                stop = threading.Event()
                def execute(number):
                    local.now = 0 if number==6000 else 180
                    work = agent.Store(self.path)
                    github = GitHub()
                    reviewer = review.Reviewer(self.tmp.name, stop)
                    def invoke(prompt, guard=None, **options):
                        reviewer.model_started = True
                        reviewer.last_usage = {'input_tokens': 11 if number==6000 else 22,
                            'cached_input_tokens': 0, 'output_tokens': 1, 'reasoning_output_tokens': 0}
                        deadlines[number] = reviewer.budget.deadline
                        barrier.wait(5)
                        if number==6000:
                            if failure=='timeout':
                                local.now=1201
                                reviewer.budget.remaining()
                            else:
                                with work.db:
                                    work.db.execute('insert into cancellations values(?,?)', (job['id'], START))
                                reviewer.cancel_guard()
                        local.now=1300  # B still has 80 seconds, independent of A's expiry.
                        reviewer.budget.remaining()
                        return result()
                    reviewer._invoke = invoke
                    reviewer.review = lambda github, evidence: reviewer.invoke('fixture', job=evidence.job_id)
                    submit = github.submit
                    def posting(job, payload):
                        row = submit(job, payload)
                        row['html_url'] = f"https://github.com/{job['repo']}/pull/{number}#pullrequestreview-123"
                        github.timeline[-1] = row
                        return row
                    github.submit = posting
                    try:
                        job = work.claim(str(number))
                        # FIFO assignment is nondeterministic; bind the behavior to the claimed PR.
                        number = job['number']
                        local.now = 0 if number==6000 else 180
                        review.review_one(self.config, work, github, reviewer, job)
                        work.release(job)
                    except BaseException as error:
                        failures.append(error)
                    finally:
                        work.db.close()
                with patch('review.time.monotonic', side_effect=lambda: getattr(local, 'now', 0)):
                    threads = [threading.Thread(target=execute, args=(n,)) for n in (6000,6001)]
                    for thread in threads: thread.start()
                    for thread in threads: thread.join(10)
                self.assertFalse(any(t.is_alive() for t in threads))
                self.assertFalse(failures)
                self.assertEqual(deadlines, {6000:1200, 6001:1380})
                self.assertFalse(stop.is_set())
                latest = {r['number']: r for r in self.store.history()}
                self.assertEqual(latest[6000]['status'], 'needs-attention' if failure=='timeout' else 'cancelled')
                self.assertEqual(latest[6001]['status'], 'submitted')
                runs = {r['number']: dict(r) for r in self.store.db.execute('select j.number,m.* from model_runs m join jobs j on j.id=m.job')}
                self.assertEqual(runs[6000]['input_tokens'], 11)
                self.assertEqual(runs[6001]['input_tokens'], 22)
                self.assertEqual(runs[6000]['status'], 'timed_out' if failure=='timeout' else 'cancelled')
                self.assertEqual(runs[6001]['status'], 'completed')
                import shutil
                shutil.rmtree(self.path.parent / 'drafts')
                with self.store.db:
                    self.store.db.execute('delete from sources')
                    self.store.db.execute('delete from model_runs')
                    self.store.db.execute('delete from jobs')
                    self.store.db.execute('delete from ledger')


if __name__ == '__main__':
    unittest.main()
