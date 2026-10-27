import json
import tempfile
import unittest
from pathlib import Path

import agent
from test_agent import SPACE, START, END, message


class QueueEstimateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = agent.Store(Path(self.tmp.name) / 'state.sqlite3')
        self.addCleanup(self.store.db.close)
        self.config = agent.Config(SPACE, START)

    def enqueue(self, name, number, created='2026-10-26T18:52:00Z'):
        self.store.ingest(self.config, [message(name, createTime=created,
            text=f'https://github.com/ROKT/launch-engineering/pull/{number}')], START, END, None)
        return self.store.db.execute('select * from jobs where number=?', (number,)).fetchone()

    def test_ack_includes_active_review_and_duplicate_reuses_position(self):
        self.enqueue('one', 6000)
        self.store.claim()
        self.enqueue('two', 6001, '2026-10-26T18:53:00Z')
        self.enqueue('duplicate', 6001, '2026-10-26T18:54:00Z')
        rows = self.store.db.execute("select payload from outbox where kind='message' order by rowid").fetchall()
        texts = [json.loads(row[0])['text'] for row in rows]
        self.assertIn('Queue: #2 / 2', texts[1])
        self.assertIn('Expected finish:', texts[1])
        self.assertIn('Likely range:', texts[1])
        self.assertIn('Provisional', texts[1])
        self.assertIn('Queue: #2 / 2', texts[2])
        self.assertEqual(len(self.store.history()), 2)

    def test_waiting_ci_has_no_fabricated_finish_time(self):
        import queue_eta
        job = self.enqueue('one', 6000)
        self.store.update(dict(job), status='waiting_ci', ready_after=2_000_000_000)
        text = queue_eta.describe(self.store, job['id'], now=agent.timestamp(END).timestamp())
        self.assertIn('Expected finish: pending CI', text)
        self.assertNotIn('~', text)

    def test_recent_history_and_elapsed_active_time_reduce_remaining_estimate(self):
        import queue_eta
        now = agent.timestamp('2026-10-26T18:55:00Z').timestamp()
        job = self.enqueue('active', 6000)
        with self.store.db:
            self.store.db.execute("update jobs set status='reviewing',updated_at='2026-10-26T18:54:00Z' where id=?", (job['id'],))
        before = queue_eta.estimate(self.store, job['id'], now=now)
        for n in range(5):
            historical = self.enqueue('history'+str(n), 6100+n)
            with self.store.db:
                self.store.db.execute("update jobs set status='submitted' where id=?", (historical['id'],))
                self.store.db.execute('insert into model_runs(id,job,started_at,finished_at,model,effort,service_tier,status) values(?,?,?,?,?,?,?,?)',
                    (str(n), historical['id'], '2026-10-26T17:00:00Z', '2026-10-26T17:03:00Z',
                     'gpt-6.1-sol', 'xhigh', 'fast', 'completed'))
        after = queue_eta.estimate(self.store, job['id'], now=now)
        self.assertLess(after['expected'], before['expected'])
        later = queue_eta.estimate(self.store, job['id'], now=now+60)
        self.assertLess(later['expected']-(now+60), after['expected']-now)

    def test_finish_times_are_eastern_and_include_date_after_midnight(self):
        import queue_eta
        job = self.enqueue('one', 6000)
        now = agent.timestamp('2026-10-27T03:59:00Z').timestamp()
        text = queue_eta.describe(self.store, job['id'], now=now)
        self.assertIn('Tue 12:', text)
        self.assertIn('AM ET', text)

    def test_preflight_refines_eta_once_without_messages_for_every_model_round(self):
        self.enqueue('one', 6000)
        job = self.store.claim()
        self.store.update(job, head='a'*40)
        self.store.start_model_run(job['id'])
        self.store.start_model_run(job['id'])
        texts = [json.loads(r[0])['text'] for r in self.store.db.execute("select payload from outbox where kind='message'")]
        updates = [t for t in texts if t.startswith('Review started')]
        self.assertEqual(len(updates), 1)
        self.assertIn('Expected finish:', updates[0])


if __name__ == '__main__':
    unittest.main()

class TwoSlotTests(QueueEstimateTests):
    def test_two_slots_fill_before_third_waits(self):
        import queue_eta
        now = agent.timestamp(END).timestamp()
        for n in (1, 2, 3):
            self.enqueue(str(n), n)
        with self.store.db:
            self.store._meta('worker_count', 2)
        rows = {r['number']: r for r in self.store.history()}
        self.assertEqual(queue_eta.estimate(self.store, rows[1]['id'], now)['expected'], now + 480)
        self.assertEqual(queue_eta.estimate(self.store, rows[2]['id'], now)['expected'], now + 480)
        self.assertEqual(queue_eta.estimate(self.store, rows[3]['id'], now)['expected'], now + 960)

    def test_next_job_uses_worker_with_less_remaining_time(self):
        import queue_eta
        now = agent.timestamp('2026-10-26T18:55:00Z').timestamp()
        for n in (1, 2, 3): self.enqueue(str(n), n)
        with self.store.db:
            self.store._meta('worker_count', 2)
            self.store.db.execute("update jobs set status='reviewing',updated_at='2026-10-26T18:50:00Z' where number=1")
            self.store.db.execute("update jobs set status='reviewing',updated_at='2026-10-26T18:54:00Z' where number=2")
        third = next(r for r in self.store.history() if r['number'] == 3)
        self.assertEqual(queue_eta.estimate(self.store, third['id'], now)['expected'], now + 660)
