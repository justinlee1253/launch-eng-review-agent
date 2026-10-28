import json
import tempfile
import unittest
from pathlib import Path

import agent
from test_agent import SPACE, START, END, message


class QueueFixture(unittest.TestCase):
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


class QueueEstimateTests(QueueFixture):
    def test_ack_includes_active_review_and_duplicate_reuses_position(self):
        self.enqueue('one', 6000)
        self.store.claim()
        self.enqueue('two', 6001, '2026-10-26T18:53:00Z')
        self.enqueue('duplicate', 6001, '2026-10-26T18:54:00Z')
        rows = self.store.db.execute("select payload from outbox where kind='message' order by rowid").fetchall()
        texts = [json.loads(row[0])['text'] for row in rows]
        self.assertIn('1 review ahead', texts[1])
        self.assertIn('ETA ~', texts[1])
        self.assertNotIn('Likely range:', texts[1])
        self.assertNotIn('Provisional', texts[1])
        self.assertNotIn('Queue:', texts[1])
        self.assertIn('1 review ahead', texts[2])
        self.assertEqual(len(self.store.history()), 2)

    def test_waiting_ci_has_no_fabricated_finish_time(self):
        import queue_eta
        job = self.enqueue('one', 6000)
        self.store.update(dict(job), status='waiting_ci', ready_after=2_000_000_000)
        text = queue_eta.describe(self.store, job['id'], now=agent.timestamp(END).timestamp())
        self.assertIn('Waiting for CI', text)
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
        updates = [t for t in texts if 'review started' in t]
        self.assertEqual(len(updates), 1)
        self.assertIn('ETA ~', updates[0])
        self.assertNotIn('ahead', updates[0])
        self.assertNotIn('Up next', updates[0])

    def test_receipt_shows_four_ahead_instead_of_a_queue_fraction(self):
        for n in range(5):
            self.enqueue('request'+str(n), 6200+n)
        text = json.loads(self.store.db.execute("select payload from outbox where kind='message' order by rowid desc limit 1").fetchone()[0])['text']
        self.assertIn('4 reviews ahead', text)
        self.assertIn('ETA ~', text)
        self.assertIn('Orbit review queued', text)
        self.assertNotIn('Falcon', text)
        self.assertNotIn('#5 / 5', text)
        self.assertEqual(len(text.splitlines()), 2)
        self.assertLess(len(text), 160)

    def test_receipt_with_no_preceding_work_says_up_next(self):
        self.enqueue('one', 6000)
        text = json.loads(self.store.db.execute("select payload from outbox where kind='message'").fetchone()[0])['text']
        self.assertIn('Up next', text)
        self.assertNotIn('0 reviews', text)

    def test_short_receipt_variants_stay_consistent_on_repeated_scans(self):
        from unittest.mock import patch
        # Different requests choose distinct phrases; rescans do not create another receipt.
        for n in range(6):
            with patch('agent.uuid.uuid4', return_value=f'00000000-0000-0000-0000-{n:012d}'):
                self.enqueue('request'+str(n), 6200+n)
        texts = [json.loads(r[0])['text'] for r in self.store.db.execute("select payload from outbox where kind='message'")]
        self.assertEqual(len({text.partition('Orbit review queued.')[0] for text in texts}), 6)
        self.enqueue('request0', 6200)
        self.assertEqual(self.store.db.execute("select count(*) from outbox where kind='message'").fetchone()[0], 6)


if __name__ == '__main__':
    unittest.main()

class TwoSlotTests(QueueFixture):
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
